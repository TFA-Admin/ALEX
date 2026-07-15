import os
import re
import subprocess
import threading
import queue
import sounddevice as sd
import numpy as np

# Piper reads "A.L.E.X." as individual letters (the dots read as sentence
# breaks). Normalize it to the plain word so it's spoken as a name, not
# spelled out — every route to speech funnels through speak() below.
ALEX_ACRONYM = re.compile(r"\bA\.\s*L\.\s*E\.\s*X\.?", re.IGNORECASE)


def _normalize_for_speech(text: str) -> str:
    return ALEX_ACRONYM.sub("Alex", text)

# Piper (and its voice model) live outside this repo — env vars let this
# run on a machine with a different drive/folder layout without editing
# code; defaults match this machine's current setup.
PIPER_PATH = os.getenv("ALEX_PIPER_PATH", r"D:\project_ALEX\piper\piper.exe")
MODEL_PATH = os.getenv(
    "ALEX_PIPER_MODEL",
    r"D:\project_ALEX\piper\models\glados_piper_medium.onnx"
)
# previous voice: r"D:\project_ALEX\piper\models\en_US-amy-medium.onnx"

speech_queue = queue.Queue()
current_process = None
process_lock = threading.Lock()
is_stopped = False

audio_level = 0.0


def play_audio_stream(pipe):
    global is_stopped, audio_level

    samplerate = 22050
    chunk_size = 1024

    with sd.RawOutputStream(
        samplerate=samplerate,
        channels=1,
        dtype='int16'
    ) as stream:

        while True:
            if is_stopped:
                break

            data = pipe.read(chunk_size * 2)
            if not data:
                break

            # compute audio level
            audio = np.frombuffer(data, dtype=np.int16)
            if len(audio) > 0:
                audio_level = float(np.abs(audio).mean()) / 32768.0

            # write raw PCM directly
            stream.write(data)


def tts_worker():
    global current_process, is_stopped

    while True:
        text = speech_queue.get()

        if text is None:
            break

        try:
            with process_lock:

                if current_process:
                    try:
                        current_process.kill()
                    except:
                        pass

                is_stopped = False

                current_process = subprocess.Popen(
                    [
                        PIPER_PATH,
                        "-m", MODEL_PATH,
                        "--output-raw",

                        "--length_scale", "1.15",   # was 1.05 — still spoke too fast
                        "--noise_scale", "0.4",     # smoother tone
                        "--noise_w", "0.6"          # reduces harshness
                    ],                    
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL
                )

                current_process.stdin.write(text.encode("utf-8"))
                current_process.stdin.close()

            play_audio_stream(current_process.stdout)

        except Exception as e:
            print("TTS error:", e)

        speech_queue.task_done()


threading.Thread(target=tts_worker, daemon=True).start()


def speak(text: str):
    if text and text.strip():
        speech_queue.put(_normalize_for_speech(text))


def stop_speaking():
    global current_process, is_stopped

    is_stopped = True

    with process_lock:
        if current_process:
            try:
                current_process.kill()
            except:
                pass
            current_process = None

    while not speech_queue.empty():
        try:
            speech_queue.get_nowait()
            speech_queue.task_done()
        except:
            break