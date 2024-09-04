import pyaudio
import google.cloud.speech_v1 as speech
import requests
import json
import tkinter as tk
from tkinter import messagebox, scrolledtext, filedialog
import threading
import queue
import pandas as pd
from docx import Document
import time
import certifi
import os
import difflib
from flask import Flask, request, jsonify
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client

TWILIO_CONFIG_FILE = "twilio_config.json"

# Set up Twilio client
TWILIO_ACCOUNT_SID = 'US74eb3979fefa519569b1750a89a5249b'
TWILIO_AUTH_TOKEN = '3fd9835033323e65fa7e0fca57c7cf64'
TWILIO_PHONE_NUMBER = '447481344041'
client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Flask app to handle incoming Twilio webhook requests
app = Flask(__name__)

API_KEYS_FILE = "api_keys.json"

# List of OpenAI API keys
openai_api_keys = []

# Global variable to keep track of which API key to use
current_key_index = 0

def load_api_keys():
    """Load API keys from a file."""
    global openai_api_keys, current_key_index
    if os.path.exists(API_KEYS_FILE):
        with open(API_KEYS_FILE, 'r') as file:
            data = json.load(file)
            openai_api_keys = data.get("api_keys", [])
            current_key_index = data.get("current_key_index", 0)
    else:
        openai_api_keys = []
        current_key_index = 0

def load_twilio_config():
    """Load Twilio configuration from a file."""
    if os.path.exists(TWILIO_CONFIG_FILE):
        with open(TWILIO_CONFIG_FILE, 'r') as file:
            return json.load(file)
    else:
        return {}

# Load API keys on startup
load_api_keys()
twilio_config = load_twilio_config()

# Audio recording parameters
RATE = 16000
CHUNK = int(RATE / 10)  # 100ms

# Global variable to control the recording
stop_recording_flag = threading.Event()

# Queue for thread-safe communication between audio processing and GUI
transcription_queue = queue.Queue()
categorization_queue = queue.Queue()

# Initialise categorisation data
categorization_data = {
    "Name": "Not provided",
    "Reason for calling": "Not provided",
    "Email address": "Not provided",
    "Phone number": "Not provided",
    "Additional notes": "Not provided"
}

# List to store transcription history
transcription_history = []

# Rate limit control
last_request_time = time.time()
rate_limit_interval = 10  # Time in seconds between API requests

# List to store selected files for appending
selected_files = []

# Flag to check if changes have been saved
changes_saved = True

def save_api_keys():
    """Save API keys to a file."""
    global openai_api_keys, current_key_index
    with open(API_KEYS_FILE, 'w') as file:
        json.dump({
            "api_keys": openai_api_keys,
            "current_key_index": current_key_index
        }, file, indent=4)

def get_next_api_key():
    """Returns the next API key and rotates the index."""
    global current_key_index
    if not openai_api_keys:
        raise ValueError("No API keys available. Please add an API key in the settings.")
    api_key = openai_api_keys[current_key_index]
    current_key_index = (current_key_index + 1) % len(openai_api_keys)
    save_api_keys()  # Save the updated index
    return api_key

def open_settings():
    """Open the settings window to manage API keys."""
    settings_window = tk.Toplevel(root)
    settings_window.title("Settings")

    # Listbox to display the API keys
    listbox_keys = tk.Listbox(settings_window, height=10, width=50)
    listbox_keys.pack(pady=10)

    # Populate the listbox with the current keys
    for key in openai_api_keys:
        listbox_keys.insert(tk.END, key)

    def add_key():
        new_key = entry_new_key.get().strip()
        if new_key:
            openai_api_keys.append(new_key)
            listbox_keys.insert(tk.END, new_key)
            entry_new_key.delete(0, tk.END)
            save_api_keys()

    def delete_key():
        selected_index = listbox_keys.curselection()
        if selected_index:
            listbox_keys.delete(selected_index)
            del openai_api_keys[selected_index[0]]
            save_api_keys()

    def save_and_close():
        save_api_keys()
        settings_window.destroy()

    # Entry field to add a new API key
    entry_new_key = tk.Entry(settings_window, width=50)
    entry_new_key.pack(pady=5)

    # Buttons to add and delete keys
    button_add_key = tk.Button(settings_window, text="Add Key", command=add_key)
    button_add_key.pack(pady=5)

    button_delete_key = tk.Button(settings_window, text="Delete Selected Key", command=delete_key)
    button_delete_key.pack(pady=5)

    button_save_and_close = tk.Button(settings_window, text="Save and Close", command=save_and_close)
    button_save_and_close.pack(pady=20)

def listen_print_loop(responses):
    """Iterates through server responses and processes them."""
    global last_request_time
    try:
        for response in responses:
            if stop_recording_flag.is_set():
                break

            if not response.results:
                continue

            result = response.results[0]
            if result.is_final:
                transcript = result.alternatives[0].transcript.strip()
                transcription_queue.put(f"Transcript: {transcript}")
                transcription_history.append(transcript)

                # Check if it's time to send the categorisation request
                if time.time() - last_request_time >= rate_limit_interval:
                    categorize_text(transcription_history)  # Send full history
                    last_request_time = time.time()
    except Exception as e:
        transcription_queue.put(f"An error occurred while transcribing: {e}")

def transcribe_streaming():
    """Streams audio input to the Google Speech API and transcribes it in real time."""
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=RATE,
        language_code="en-GB",
    )

    streaming_config = speech.StreamingRecognitionConfig(
        config=config,
        interim_results=True
    )

    p = pyaudio.PyAudio()
    stream = None
    try:
        stream = p.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=RATE,
            input=True,
            frames_per_buffer=CHUNK,
        )

        audio_generator = (stream.read(CHUNK) for _ in iter(int, 1) if not stop_recording_flag.is_set())
        requests = (speech.StreamingRecognizeRequest(audio_content=content) for content in audio_generator)

        responses = client.streaming_recognize(config=streaming_config, requests=requests)
        listen_print_loop(responses)
    except Exception as e:
        transcription_queue.put(f"An error occurred while transcribing: {e}")
    finally:
        if stream is not None and stream.is_active():
            stream.stop_stream()
            stream.close()
        p.terminate()

def categorize_text(transcription_history):
    """Uses OpenAI to extract and categorise information from the transcribed text."""
    global last_request_time

    full_transcription = "\n".join(transcription_history)

    prompt = f"""
    You are a virtual assistant helping to extract important information from a transcribed phone call. Your job is to extract and categorise the following information from the transcribed text of the phone call. 
    You must acknowledge the fact that the transcribed text will not always be exact based on the person's accent. You should use the context of the sentence and conversation to decipher the actual words that were meant to be transcribed and categorise that. 
    The text may contain information about a customer calling a business. If you do address the customer, you will address the customer as "they"/"them", not "it".

    Please identify and extract:
    - Name
    - Reason for calling (try to identify the main reason in a single sentence. Try to understand the whole phone call instead of focusing on one thing they have said. The reason may not be clear from the start)
    - Email address
    - Phone number (if available)
    - Any additional notes from a sales perspective. You can make this multiple sentences. Do not reiterate the information in the categories prior to this one. just be judgmental here and summarise the conversations tone.

    If any information is missing, do not remove existing data but leave that field unchanged. Provide the results in a JSON format.

    Text:
    {full_transcription}
    """

    api_key = get_next_api_key()

    try:
        response = requests.post(
            'https://api.openai.com/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json'
            },
            json={
                "model": "gpt-3.5-turbo",
                "messages": [{"role": "system", "content": prompt}],
                "max_tokens": 500
            }
        )

        if response.status_code == 200:
            result = response.json()
            choices = result.get("choices", [])
            if choices:
                content = choices[0].get("message", {}).get("content", "")
                try:
                    extracted_data = json.loads(content)
                    categorization_queue.put(extracted_data)
                except json.JSONDecodeError:
                    transcription_queue.put("Failed to decode categorization response.")
        else:
            transcription_queue.put(f"Error categorizing text: {response.text}")

    except requests.RequestException as e:
        transcription_queue.put(f"Request error while categorizing text: {e}")

def start_transcription():
    """Starts transcription and categorization threads."""
    stop_recording_flag.clear()
    transcription_thread = threading.Thread(target=transcribe_streaming)
    transcription_thread.start()

def stop_transcription():
    """Stops the transcription process."""
    stop_recording_flag.set()
    transcription_queue.put("Transcription stopped.")
    categorization_queue.put("Categorization stopped.")

def save_transcription_to_file():
    """Saves the transcriptions to a file."""
    global changes_saved
    file_path = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=[("Text files", "*.txt")])
    if file_path:
        with open(file_path, 'w') as file:
            for entry in transcription_history:
                file.write(f"{entry}\n")
        changes_saved = True

def append_transcription_to_file():
    """Appends the transcriptions to an existing file."""
    global changes_saved
    file_path = filedialog.askopenfilename(defaultextension=".txt", filetypes=[("Text files", "*.txt")])
    if file_path:
        with open(file_path, 'a') as file:
            for entry in transcription_history:
                file.write(f"{entry}\n")
        changes_saved = True

def save_categorization_to_file():
    """Saves the categorization data to a file."""
    global changes_saved
    file_path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON files", "*.json")])
    if file_path:
        with open(file_path, 'w') as file:
            json.dump(categorization_data, file, indent=4)
        changes_saved = True

def append_categorization_to_file():
    """Appends the categorization data to an existing file."""
    global changes_saved
    file_path = filedialog.askopenfilename(defaultextension=".json", filetypes=[("JSON files", "*.json")])
    if file_path:
        with open(file_path, 'a') as file:
            json.dump(categorization_data, file, indent=4)
        changes_saved = True

def on_closing():
    """Handle window closing event."""
    if not changes_saved:
        if messagebox.askokcancel("Quit", "You have unsaved changes. Are you sure you want to quit?"):
            root.destroy()
    else:
        root.destroy()

def update_transcription_display():
    """Update the transcription display with new data."""
    while True:
        try:
            transcription = transcription_queue.get_nowait()
            text_display.insert(tk.END, f"{transcription}\n")
            text_display.yview(tk.END)
        except queue.Empty:
            break
        root.after(100, update_transcription_display)

def update_categorization_display():
    """Update the categorization display with new data."""
    while True:
        try:
            categorization = categorization_queue.get_nowait()
            categorization_display.delete(1.0, tk.END)
            categorization_display.insert(tk.END, json.dumps(categorization, indent=4))
        except queue.Empty:
            break
        root.after(100, update_categorization_display)

# Declare the root variable as global
def on_closing():
    """Handle window closing event."""
    global root
    if not changes_saved:
        if messagebox.askokcancel("Quit", "You have unsaved changes. Are you sure you want to quit?"):
            root.destroy()
    else:
        root.destroy()

def start_gui():
    """Initialise the GUI."""
    global root, text_display, categorization_display
    root = tk.Tk()
    root.title("Real-time Transcription and Categorization")

    # Text area for displaying transcriptions
    text_display = scrolledtext.ScrolledText(root, wrap=tk.WORD, height=20, width=80)
    text_display.pack(padx=10, pady=10)

    # Text area for displaying categorization results
    categorization_display = scrolledtext.ScrolledText(root, wrap=tk.WORD, height=10, width=80)
    categorization_display.pack(padx=10, pady=10)

    # Buttons for controlling transcription
    frame_controls = tk.Frame(root)
    frame_controls.pack(pady=5)

    button_start = tk.Button(frame_controls, text="Start Transcription", command=start_transcription)
    button_start.pack(side=tk.LEFT, padx=5)

    button_stop = tk.Button(frame_controls, text="Stop Transcription", command=stop_transcription)
    button_stop.pack(side=tk.LEFT, padx=5)

    button_save_transcription = tk.Button(frame_controls, text="Save Transcription", command=save_transcription_to_file)
    button_save_transcription.pack(side=tk.LEFT, padx=5)

    button_append_transcription = tk.Button(frame_controls, text="Append Transcription", command=append_transcription_to_file)
    button_append_transcription.pack(side=tk.LEFT, padx=5)

    button_save_categorization = tk.Button(frame_controls, text="Save Categorization", command=save_categorization_to_file)
    button_save_categorization.pack(side=tk.LEFT, padx=5)

    button_append_categorization = tk.Button(frame_controls, text="Append Categorization", command=append_categorization_to_file)
    button_append_categorization.pack(side=tk.LEFT, padx=5)

    # Menu for settings
    menu_bar = tk.Menu(root)
    root.config(menu=menu_bar)
    settings_menu = tk.Menu(menu_bar, tearoff=0)
    menu_bar.add_cascade(label="Settings", menu=settings_menu)
    settings_menu.add_command(label="Manage API Keys", command=open_settings)

    root.protocol("WM_DELETE_WINDOW", on_closing)
    root.after(100, update_transcription_display)
    root.after(100, update_categorization_display)
    root.mainloop()

def categorize_and_save():
    """Categorize the text and save the result."""
    global categorization_data
    categorize_text(transcription_history)
    if categorization_queue.empty():
        categorization_data = {
            "Name": "Not provided",
            "Reason for calling": "Not provided",
            "Email address": "Not provided",
            "Phone number": "Not provided",
            "Additional notes": "Not provided"
        }
    else:
        categorization_data = categorization_queue.get()

if __name__ == "__main__":
    start_gui()
