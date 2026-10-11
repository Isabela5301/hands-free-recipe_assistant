#Import libraries and recipe from an different library
import json
import math
import queue
import sys
import textwrap
import threading
import time
from collections import deque

#import the libraries needed for the program to run
import cv2
import mediapipe as mp
import pythoncom
import serial
import win32com.client
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision
from serial.tools import list_ports

#arduino port to use, if None it will try to find one automatically
ARDUINO_PORT = None

#Recipe file to use, if no argument is given it will use the demo recipe
RECIPE_FILE = sys.argv[1] if len(sys.argv) > 1 else "recipe_demo.json"

#Demo mode is now set to false, so the timers will be real
DEMO_MODE = True           # <-- CHANGED: real timers now 
DEMO_TIMER_SECONDS = 10
#Gestures that can be used to control the recipe assistant, and their corresponding actions
ACTION_GESTURES = {
    "Thumb_Up": "next",
    "Thumb_Down": "back",
    "Open_Palm": "repeat",
    "Pointing_Up": "timer",
    "Closed_Fist": "cancel",
}
#Safety and timing parameters for gesture recognition and actions
HOLD_SECONDS = 0.35
HOLD_OVERRIDES = {"Open_Palm": 1.2}
COOLDOWN_SECONDS = 0.5
GRACE_SECONDS = 0.2
MIN_SCORE = 0.6
ALARM_SECONDS = 5
FADE_SECONDS = 0.3
VOICE_RATE = 1
#Safety and timing parameters for face detection and swipe gestures
REQUIRE_LOOK = True
SHOW_FACE_DEBUG = False      # <-- CHANGED
YAW_RANGE = (0.35, 0.65)
PITCH_RANGE = (0.40, 0.85) # Claude suggested changing this to (0.35, 0.85) to allow for a wider range of pitch angles, but I kept it as is for now
LOOK_GRACE = 0.6
FACE_EVERY_N_FRAMES = 3
#swipe gesture parameters
SWIPE_WINDOW = 0.5
SWIPE_DISTANCE = 0.18
SWIPE_MIN_SPEED = 0.5
SWIPE_MIN_SAMPLES = 3
SWIPE_COOLDOWN = 1.0
SHOW_SWIPE_DEBUG = False     ## <-- CHANGED

#Recipe loading and processing functions
def load_recipe(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)

#Convert common abbreviations in ingredient text to spoken form
def spoken_units(text):
    return (text.replace("1 tsp", "1 teaspoon")
                .replace("1 tbsp", "1 tablespoon")
                .replace("tsp", "teaspoons")
                .replace("tbsp", "tablespoons"))

#Build a list of steps for the recipe, including ingredients and timers
def build_steps(recipe):
    ingredients = recipe["ingredients"]
    spoken = ", ".join(spoken_units(item) for item in ingredients)
    steps = [{
        "text": "You will need: " + spoken + ". Thumbs up to start.",
        "shown": "Ingredients for " + recipe["name"] + ". Thumbs up to start.",
        "timer": 0,
        "list": ingredients,
    }]
    for number, item in enumerate(recipe["steps"], start=1):
        seconds = item.get("timer", 0)
        if DEMO_MODE:
            seconds = min(seconds, DEMO_TIMER_SECONDS)
        steps.append({
            "text": f"Step {number}. {item['text']}",
            "timer": seconds,
        })
    return steps


try:
    RECIPE = load_recipe(RECIPE_FILE)
    STEPS = build_steps(RECIPE)
except Exception as error:
    print("Could not load", RECIPE_FILE, "-", error)
    sys.exit(1)
# INGREDIENTS is a list of ingredients from the recipe, used for display and speech
INGREDIENTS = RECIPE["ingredients"]

speech_queue = queue.Queue()

# Speech worker function that runs in a separate thread to handle text-to-speech requests
def speech_worker():                      # <-- CHANGED: whole function
    pythoncom.CoInitialize()
    voice = win32com.client.Dispatch("SAPI.SpVoice")
    voice.Rate = VOICE_RATE
    try:
        voice.Speak("<silence msec='10'/>", 1 | 8)
    except Exception:
        pass
    pending = None
    while True:
        if pending is None:
            try:
                pending = speech_queue.get(timeout=0.05)
            except queue.Empty:
                pythoncom.PumpWaitingMessages()
                continue
        text, asked_at = pending
        pending = None
        # keep only the newest waiting message
        while True:
            try:
                text, asked_at = speech_queue.get_nowait()
            except queue.Empty:
                break
        voice.Speak(text, 1 | 2)          # start talking right away
        lag_ms = int((time.time() - asked_at) * 1000)
        print(f"[voice] started {lag_ms} ms after the request")
        # stay here until it finishes, so nothing cuts it off by accident
        while voice.Status.RunningState == 2:   # 2 = still speaking
            pythoncom.PumpWaitingMessages()
            try:
                pending = speech_queue.get(timeout=0.05)
                break                      # a newer message arrived, switch to it
            except queue.Empty:
                pass


def speak(text):
    speech_queue.put((text, time.time()))


threading.Thread(target=speech_worker, daemon=True).start()

# Serial port detection and Arduino connection functions
def list_serial_ports():
    ports = list(list_ports.comports())
    print("Serial ports found:")
    for port in ports:
        print("  ", port.device, "-", port.description)
    if not ports:
        print("   (none)")
    return ports


def find_arduino_port():
    for port in list_serial_ports():
        info = (port.description + " " + (port.manufacturer or "")).lower()
        if "arduino" in info or "ch340" in info or "usb serial" in info:
            return port.device
    return None


arduino = None
port_name = ARDUINO_PORT or find_arduino_port()
if port_name is None:
    print("No Arduino found. Check the USB cable, and close the Serial Monitor.")
else:
    try:
        arduino = serial.Serial(port_name, 9600, timeout=1, write_timeout=1)
        time.sleep(2)
        arduino.write(b"C\n")
        print("Arduino connected on", port_name)
    except Exception as error:
        print("Could not open", port_name, "-", error)
        list_serial_ports()
        arduino = None

serial_queue = queue.Queue()

# Serial worker function that runs in a separate thread to handle sending commands to the Arduino
def serial_worker():
    global arduino
    while True:
        command = serial_queue.get()
        if arduino is None:
            continue
        try:
            arduino.write((command + "\n").encode())
        except Exception as error:
            print("Lost connection to Arduino:", error)
            arduino = None

# Function to send a command to the Arduino via the serial queue    
def send(command):
    serial_queue.put(command)


threading.Thread(target=serial_worker, daemon=True).start()

# Function to show the current step on the Arduino display
def show_step(index):
    if index == 0:
        send("SIngredients")
    else:
        send(f"S{index}/{len(STEPS) - 1}")

# Function to format time in seconds to a string in the format "minutes:seconds"
def format_time(seconds):
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}:{secs:02d}"

# Function to convert time in seconds to a spoken string format
def spoken_time(seconds):
    minutes, secs = divmod(seconds, 60)
    parts = []
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if secs:
        parts.append(f"{secs} second{'s' if secs != 1 else ''}")
    return " and ".join(parts)

# Function to handle timer completion, sending a notification and triggering the alarm
def timer_finished():
    print("TIMER DONE")
    speak("Time is up!")
    send("T0:00")
    send("B")


def draw_cheat_sheet(frame):
    height, width = frame.shape[:2]
    lines = [
        "HAND GUIDE",
        "Thumb up / swipe right: next",
        "Thumb down / swipe left: back",
        "Open palm, hold 1s: repeat",
        "Point up: start timer",
        "Fist: cancel timer",
        "Swipes need you to look at",
        "the camera",
        "h guide    i ingredients",
    ]
    top = 78
    x = width - 270
    panel_bottom = top + 20 * len(lines) + 12
    cv2.rectangle(frame, (x, top), (width - 10, panel_bottom), (0, 0, 0), -1)
    for i, line in enumerate(lines):
        color = (0, 255, 255) if i == 0 else (255, 255, 255)
        cv2.putText(frame, line, (x + 10, top + 22 + i * 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)

# the list of ingredients on the frame, limiting the number of displayed items and showing a summary if there are too many
def draw_ingredients(frame, ingredients):
    items = list(ingredients)
    if len(items) > 7:
        items = items[:6] + [f"...and {len(items) - 6} more"]
    panel_bottom = 168 + 22 * len(items)
    cv2.rectangle(frame, (10, 134), (340, panel_bottom), (0, 0, 0), -1)
    cv2.putText(frame, "Ingredients", (20, 156),
                cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 255), 2)
    for i, item in enumerate(items):
        cv2.putText(frame, "- " + item, (20, 180 + i * 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

# Function to determine if the hand is open based on the positions of the fingertips and the wrist
def hand_is_open(points):
    wrist = points[0]
    open_fingers = 0
    for tip, pip in [(8, 6), (12, 10), (16, 14), (20, 18)]:
        tip_distance = math.hypot(points[tip].x - wrist.x,
                                  points[tip].y - wrist.y)
        pip_distance = math.hypot(points[pip].x - wrist.x,
                                  points[pip].y - wrist.y)
        if tip_distance > pip_distance:
            open_fingers += 1
    return open_fingers >= 3

# Function to detect swipe gestures based on recent hand samples, checking for sufficient movement and speed in the horizontal direction while ensuring the hand is mostly sideways
def detect_swipe(samples, now):
    recent = [s for s in samples if now - s[0] <= SWIPE_WINDOW and s[3]]
    if len(recent) < SWIPE_MIN_SAMPLES or now - recent[-1][0] > 0.25:
        return None, 0.0
    first, last = recent[0], recent[-1]
    dx = last[1] - first[1]
    dy = last[2] - first[2]
    seconds = max(last[0] - first[0], 0.05)
    fast_enough = abs(dx) / seconds >= SWIPE_MIN_SPEED
    mostly_sideways = abs(dy) <= abs(dx) * 0.75 # Check that the movement is mostly horizontal
    if abs(dx) >= SWIPE_DISTANCE and fast_enough and mostly_sideways:
        return ("right" if dx > 0 else "left"), dx #Claude to return the direction of the swipe and the distance moved in the x-direction
    return None, dx

# CameraStream class to handle video capture in a separate thread
class CameraStream:
    def __init__(self, index=0):
        self.cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        self.cap.set(cv2.CAP_PROP_FPS, 30)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self.frame = None
        self.frame_id = 0
        self.running = True
        self.lock = threading.Lock()
        threading.Thread(target=self.update, daemon=True).start()
# Update the camera stream by continuously reading frames in a separate thread
    def update(self):
        while self.running:
            success, frame = self.cap.read()
            if success:
                with self.lock:
                    self.frame = frame
                    self.frame_id += 1
# Read the latest frame from the camera stream, returning the frame ID and a copy of the frame if it's new
    def read(self, last_id):
        with self.lock:
            if self.frame is None or self.frame_id == last_id:
                return last_id, None
            return self.frame_id, self.frame.copy()
# Stop the camera stream and release resources
    def stop(self):
        self.running = False
        time.sleep(0.2)
        self.cap.release()

# Initialize the latest gesture and face recognition results, a deque to store recent hand samples, and a lock for thread-safe access to the hand samples
latest = {"gesture": ("None", 0.0), "face": None}
hand_samples = deque(maxlen=90)
hand_lock = threading.Lock()

# Callback functions for gesture and face recognition results
def on_gesture_result(result, output_image, timestamp_ms):
    if result.gestures:
        top = result.gestures[0][0]
        latest["gesture"] = (top.category_name, top.score)
    else:
        latest["gesture"] = ("None", 0.0)

    if result.hand_landmarks:
        points = result.hand_landmarks[0]
        palm_x = (points[0].x + points[5].x + points[17].x) / 3
        palm_y = (points[0].y + points[5].y + points[17].y) / 3
        with hand_lock:
            hand_samples.append(
                (time.time(), palm_x, palm_y, hand_is_open(points)))

# Claude to handle face recognition results, calculating yaw and pitch to determine if the user is looking at the camera
def on_face_result(result, output_image, timestamp_ms):
    value = None
    if result.face_landmarks:
        points = result.face_landmarks[0]
        edge_xs = (points[234].x, points[454].x)
        left, right = min(edge_xs), max(edge_xs)
        top, chin = points[10].y, points[152].y
        face_width = right - left
        face_height = chin - top
        if face_width >= 0.02 and face_height >= 0.02:
            yaw = (points[1].x - left) / face_width
            pitch = (points[1].y - top) / face_height
            facing = (YAW_RANGE[0] <= yaw <= YAW_RANGE[1]
                      and PITCH_RANGE[0] <= pitch <= PITCH_RANGE[1])
            value = (yaw, pitch, facing, time.time())
    latest["face"] = value

#Claude to set up the gesture recognizer with the specified options, including the model path, running mode, number of hands to detect, and the callback function for processing results
gesture_options = vision.GestureRecognizerOptions(
    base_options=mp_python.BaseOptions(
        model_asset_path="gesture_recognizer.task"),
    running_mode=vision.RunningMode.LIVE_STREAM,
    num_hands=1,
    result_callback=on_gesture_result,
)
recognizer = vision.GestureRecognizer.create_from_options(gesture_options)
# Initialize the face landmarker to None, and if REQUIRE_LOOK is True, attempt to create a face landmarker with the specified options. If the face model cannot be loaded, print an error message and disable swipe gestures.
face_landmarker = None
if REQUIRE_LOOK:
    try:
        face_options = vision.FaceLandmarkerOptions(
            base_options=mp_python.BaseOptions(
                model_asset_path="face_landmarker.task"),
            running_mode=vision.RunningMode.LIVE_STREAM,
            num_faces=1,
            result_callback=on_face_result,
        )
        face_landmarker = vision.FaceLandmarker.create_from_options(
            face_options) # Claude to set up the face landmarker with the specified options, including the model path, running mode, number of faces to detect, and the callback function for processing results
    except Exception as error:
        print("Face model could not load, so swipe is turned OFF:", error)

# Determine if swipe gestures are enabled based on whether the face model is loaded and the REQUIRE_LOOK setting
swipe_enabled = (not REQUIRE_LOOK) or (face_landmarker is not None)

# Initialize the camera stream and various state variables for gesture recognition, face detection, and recipe step management
camera = CameraStream()
# claude to initialize various state variables for gesture recognition, face detection, and recipe step management
last_frame_id = -1
last_timestamp = 0
frame_count = 0
fps = 0.0
fps_frames = 0
fps_start = time.time()
current_step = 0
step_changed_at = time.time()
held_gesture = "None"
hold_start = time.time()
last_seen = 0
fired = False
last_action_time = 0
timer_end = None
alarm_until = 0
last_sent_remaining = None
show_help = True
show_ingredients = False

# Initialize variables for swipe detection, face detection, and gesture recognition
last_swipe_time = 0
last_looking = 0
warn_until = 0
peak_dx = 0.0
peak_time = 0

speak(STEPS[current_step]["text"])
show_step(current_step)

while True:
    now = time.time()

    if timer_end is not None and now >= timer_end:
        timer_end = None
        alarm_until = now + ALARM_SECONDS
        timer_finished()

    if alarm_until and now >= alarm_until:
        alarm_until = 0
        send("X")
# Update the Arduino display with the remaining time if a timer is running, and send a notification if the remaining time has changed since the last update
    if timer_end is not None:
        remaining = max(0, math.ceil(timer_end - now))
        if remaining != last_sent_remaining:
            send(f"T{format_time(remaining)}")
            last_sent_remaining = remaining
# Read a frame from the camera stream, and if it's valid, process it for gesture and face recognition
    frame_id, frame = camera.read(last_frame_id)
    if frame is None:
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break
        continue
    last_frame_id = frame_id
# Flip the frame horizontally for a mirror effect and get its dimensions
    frame = cv2.flip(frame, 1)
    height, width = frame.shape[:2]
    now = time.time()

    frame_count += 1
    fps_frames += 1
    if now - fps_start >= 1.0:
        fps = fps_frames / (now - fps_start)
        fps_frames = 0
        fps_start = now
# Convert the frame from BGR to RGB format and create a MediaPipe Image object for gesture recognition
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
    timestamp_ms = max(int(now * 1000), last_timestamp + 1)
    last_timestamp = timestamp_ms
    recognizer.recognize_async(mp_image, timestamp_ms)
    if face_landmarker is not None and frame_count % FACE_EVERY_N_FRAMES == 0:
        face_landmarker.detect_async(mp_image, timestamp_ms)

    name, score = latest["gesture"]
    raw = name if score >= MIN_SCORE else "None"
# Handle gesture recognition and holding logic, determining if the current gesture should be considered held based on the grace period and last seen time
    if raw != "None":
        last_seen = now
    if raw == "None" and now - last_seen < GRACE_SECONDS:
        gesture = held_gesture
    else:
        gesture = raw

    if gesture != held_gesture:
        held_gesture = gesture
        hold_start = now
        fired = False
# Calculate how long the current gesture has been held, determine the required hold time for the gesture, and check if the cooldown period has passed since the last action
    held_for = now - hold_start
    needed = HOLD_OVERRIDES.get(gesture, HOLD_SECONDS)
    ready = now - last_action_time >= COOLDOWN_SECONDS
# Handle face detection and looking status, determining if the user is looking at the camera based on yaw and pitch values
    face = latest["face"]
    face_fresh = face is not None and now - face[3] < 0.8
    looking_now = (not REQUIRE_LOOK) or (face_fresh and face[2])
    if looking_now:
        last_looking = now
    looking_ok = now - last_looking <= LOOK_GRACE
# Handle swipe detection and cooldown, checking if the user is looking at the camera before accepting the swipe action
    with hand_lock:
        samples = list(hand_samples)
    swipe, swipe_dx = (None, 0.0)
    if swipe_enabled:
        swipe, swipe_dx = detect_swipe(samples, now)
    hand_moving = abs(swipe_dx) > 0.08
    if swipe is not None and now - last_swipe_time < SWIPE_COOLDOWN:
        swipe = None
# Update the peak swipe distance and time if the current swipe distance is greater than the previous peak or if more than 1 second has passed since the last peak
    if abs(swipe_dx) >= abs(peak_dx) or now - peak_time > 1.0:
        peak_dx = swipe_dx
        peak_time = now

    action = None
# Handle swipe gestures, checking if the user is looking at the camera before accepting the swipe action
    if swipe is not None:
        last_swipe_time = now
        last_action_time = now
        fired = True
        with hand_lock:
            hand_samples.clear()
        if looking_ok:
            action = "next" if swipe == "right" else "back"
            print(f"SWIPE {swipe} accepted (move {swipe_dx:+.2f})")
        else:
            warn_until = now + 1.5
            print("SWIPE ignored: look at the camera first")

    if (action is None and gesture in ACTION_GESTURES
            and held_for >= needed and ready and not fired
            and not (gesture == "Open_Palm" and hand_moving)):
        action = ACTION_GESTURES[gesture]
        fired = True
# Handle the action based on the gesture or swipe detected, updating the current step, starting/cancelling timers, and providing feedback to the user
    if action is not None:
        message = STEPS[current_step]["text"]
        if action == "next":
            if current_step < len(STEPS) - 1:
                current_step += 1
                step_changed_at = now
                message = STEPS[current_step]["text"]
                show_step(current_step)
            else:
                message = "That was the last step. Enjoy your meal!"
        elif action == "back":
            if current_step > 0:
                current_step -= 1
                step_changed_at = now
                show_step(current_step)
            message = STEPS[current_step]["text"]
        elif action == "timer":
            seconds = STEPS[current_step]["timer"]
            if seconds > 0:
                timer_end = now + seconds
                last_sent_remaining = None
                message = f"Timer started. {spoken_time(seconds)}."
            else:
                message = "This step has no timer."
        elif action == "cancel":
            if timer_end is not None:
                timer_end = None
                send("X")
                message = "Timer cancelled."
            else:
                message = "No timer is running."
        speak(message)
        send("C")
        last_action_time = now
# Display the current gesture on the frame, along with a progress bar for gestures that require holding
    cv2.putText(frame, gesture, (20, 40), cv2.FONT_HERSHEY_SIMPLEX,
                1, (0, 255, 0), 2)
    if gesture in ACTION_GESTURES and not fired:
        progress = min(held_for / needed, 1.0)
        cv2.rectangle(frame, (20, 55), (20 + int(200 * progress), 70),
                      (0, 255, 0), -1)
# Display the current gesture and its status on the frame, along with a progress bar for gestures that require holding
    if REQUIRE_LOOK:
        if face_landmarker is None:
            cv2.putText(frame, "Swipe: OFF (no face model)", (20, 95),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 165, 255), 2)
        elif looking_ok:
            cv2.putText(frame, "Looking: YES", (20, 95),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2)
        else:
            cv2.putText(frame, "Looking: NO", (20, 95),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 255), 2)
        if SHOW_FACE_DEBUG and face_fresh:
            cv2.putText(frame, f"yaw {face[0]:.2f}  pitch {face[1]:.2f}",
                        (width - 260, 66), cv2.FONT_HERSHEY_SIMPLEX,
                        0.5, (200, 200, 200), 1)
# Display the current frames per second (FPS) on the frame for performance monitoring
    cv2.putText(frame, f"FPS {fps:.0f}", (190, 95),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (200, 200, 200), 1)
    if SHOW_SWIPE_DEBUG and swipe_enabled:
        cv2.putText(frame,
                    f"Swipe move {peak_dx:+.2f} (need {SWIPE_DISTANCE:.2f})",
                    (20, 122), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5, (200, 200, 200), 1)
# Display the timer countdown if a timer is running, and show a "TIME'S UP!" message with a red border if the alarm is active
    if timer_end is not None:
        remaining = max(0, math.ceil(timer_end - now))
        cv2.putText(frame, "Timer " + format_time(remaining),
                    (width - 260, 40), cv2.FONT_HERSHEY_SIMPLEX,
                    1, (0, 200, 255), 2)
# Display a red border and "TIME'S UP!" message if the alarm is active, with a flashing effect based on the current time
    if now < alarm_until and int(now * 4) % 2 == 0:
        cv2.rectangle(frame, (0, 0), (width - 1, height - 1), (0, 0, 255), 12)
        cv2.putText(frame, "TIME'S UP!", (width // 2 - 130, height // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 0, 255), 4)

    if STEPS[current_step].get("list") or show_ingredients:
        draw_ingredients(frame, INGREDIENTS)
# Display the current step text at the bottom of the frame, with a fade-in effect based on how long it's been since the step changed
    cv2.rectangle(frame, (0, height - 150), (width, height), (0, 0, 0), -1)
    fade = min((now - step_changed_at) / FADE_SECONDS, 1.0)
    shade = int(255 * fade)
    shown = STEPS[current_step].get("shown", STEPS[current_step]["text"])
    lines = textwrap.wrap(shown, 50)
    for i, line in enumerate(lines):
        cv2.putText(frame, line, (15, height - 115 + i * 28),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (shade, shade, shade), 2)

    step_timer = STEPS[current_step]["timer"]
    if step_timer > 0 and timer_end is None:
        hint = f"Point up to start {format_time(step_timer)} timer"
        cv2.putText(frame, hint, (15, height - 15),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)

    if show_help:
        draw_cheat_sheet(frame)
# Display warning message if the user is not looking at the camera
    if now < warn_until:
        cv2.rectangle(frame, (width // 2 - 230, height // 2 - 45),
                      (width // 2 + 230, height // 2 + 15), (0, 0, 0), -1)
        cv2.putText(frame, "Look at the camera to swipe",
                    (width // 2 - 215, height // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 200, 255), 2)
# Display the video frame with the overlayed information and wait for user input to quit or toggle help/ingredients display
    cv2.imshow("Hands-Free Recipe Assistant", frame)

    key = cv2.waitKey(1) & 0xFF
    if key == ord("q"):
        break
    if key == ord("h"):
        show_help = not show_help
    if key == ord("i"):
        show_ingredients = not show_ingredients
# Cleanup and exit
camera.stop()
recognizer.close()
if face_landmarker is not None:
    face_landmarker.close()
send("X")
time.sleep(0.3)
if arduino is not None:
    arduino.close()
cv2.destroyAllWindows()