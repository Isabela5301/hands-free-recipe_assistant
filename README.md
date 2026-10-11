# Hands-Free Recipe Assistant

A computer vision recipe assistant you control with hand gestures, so you never touch a screen with messy hands.

**Demo video:https://www.youtube.com/watch?v=AqSJ-2K7KCY** 

## What it does
- Shows an ingredient list, then walks you through the recipe one step at a time
- Reads every step aloud and announces timers
- Gesture control: thumbs up or swipe right = next step, thumbs down or swipe left = back, open palm (hold) = repeat, point up = start timer, fist = cancel timer
- Attention safety check: swipes only count when you are looking at the camera, so reaching across the counter does not skip steps
- An Arduino shows the step number and countdown on an LCD and sounds a buzzer when a timer ends
- Works offline, and every tool used is free

## How it works

1. A camera feeds frames to MediaPipe's pretrained gesture recognizer (hand landmarks and gesture classes)
2. A second MediaPipe model (face landmarks) estimates whether you are facing the camera
3. Custom logic adds hold times, cooldowns, swipe detection (speed and distance of the palm), and the look-at-camera rule
4. Windows speech reads steps aloud from its own thread, so the video never freezes
5. Python sends short serial messages to the Arduino: S3/8 for the step, T1:15 for the timer, C for a chirp, B for the alarm

## Hardware

- Windows laptop with a webcam
- Arduino Uno, LCD1602 (16 pins, wired directly), passive buzzer, breadboard, jumper wires, potentiometer for contrast and 1K ohm resistor, 220 ohm resistor

| Arduino | Connects to |
|---|---|
| D12 | LCD RS (pin 4) |
| D11 | LCD E (pin 6) |
| D5, D4, D3, D2 | LCD D4, D5, D6, D7 (pins 11 to 14) |
| D8 | Buzzer + |
| 5V and GND | LCD power pins, backlight (through 220 ohm), buzzer - |

Upload `arduino/recipe_display/recipe_display.ino` with the Arduino IDE first.

## Setup

```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python recipe_assistant.py
```

Use another recipe by naming its file: `python recipe_assistant.py my_recipe.json`

If the Arduino is not found automatically, set `ARDUINO_PORT` near the top of `recipe_assistant.py` (for example "COM3"). The app also runs without the Arduino.

## Controls

Press h to hide the gesture guide, i to show the ingredients, q to quit. `DEMO_MODE = True` caps timers at 10 seconds for demonstrations.

## Limitations

- Needs decent lighting, and is tuned for one hand
- Head direction is a proxy for "looking at the camera", not exact eye tracking
- Windows only (it uses the built-in Windows voice)

## Open-source credits
MediaPipe, OpenCV, pyserial, pywin32, and the Arduino LiquidCrystal library, used under their open-source licenses.
## AI disclosure and attribution
 **AI in the product:** two pretrained Google MediaPipe models run locally: a gesture recognizer (hand landmarks and gesture classes) and a face landmarker (head direction for the attention check). I did not train them.
 **AI used to help build it:** 
I used visual Studio code copiolet as an coding assistants also as Claude (Anthropic's AI assistant) as coding assistant,It suggetsed some new concept, debugging and redrafting document. I wrote some of the code in, ran it, debugged it, and tuned it on my own hardware.
| Work | Done by |
|---|---|
| Choosing and designing the project, recipe content, demo | Me |
| Arduino wiring, LCD, buzzer, hardware testing | Me |
| Some code draft, README template | Claude |
|  some Writing, Typing, running, tuning, and fixing the code | Me |
| Gesture and face models | Google MediaPipe |
| Libraries | OpenCV, pyserial, pywin32, Arduino LiquidCrystal, Windows built-in speech |
## Ethical AI and privacy

- All video is processed locally on the laptop. Nothing is recorded, saved, or uploaded, and no account is needed.
- The face model only estimates which way the head is pointing. It does not identify anyone.
- This is a convenience tool, not a medical or safety device.

## Open-source credits

MediaPipe, OpenCV, pyserial, pywin32, and the Arduino LiquidCrystal library, used under their open-source licenses.

Also change the first README line to say it was built for