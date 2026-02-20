
AMBIENT-APP/
├── core/
│   ├── __init__.py
│   ├── config.py
│   └── models.py
│
├── web_server/
│   ├── __init__.py
│   ├── main.py
│   └── websocket_manager.py
│
├── worker/
│   ├── __init__.py
│   ├── main.py
│   └── processor.py
│
├── static/
│   ├── index.html
│   └── js/
│       └── main.js
│
├── uploads/
│   ├── audio/
│   │   └── .gitkeep
│   └── transcripts/
│       └── .gitkeep
│
├── .env
├── .gitignore
├── README.md


### Architecture - 
[ User's Browser (Frontend) ]
         |
         | 1. WebSocket Connection: Streams audio chunks in real-time.
         |    Sends control messages like "pause" and "stop".
         V
+---------------------------------+
|   Web Server Process (FastAPI)  |
|  - Manages WebSocket connection.|
|  - Receives audio chunks.       |
+---------------------------------+
         |
         | 2. Task Creation: Places "process audio" jobs into the queue.
         V
+---------------------------------+
|   Task Queue (e.g., Redis)      |
|  - Decouples Web Server & Worker|
+---------------------------------+
         ^
         | 3. Task Consumption: The Worker constantly listens for new jobs.
         |
+---------------------------------+
|   Worker Process (Python)       |
|  - Runs VAD to filter silence.  |
|  - Saves speech to audio file.  | ----> [ Final Audio File (.wav) ]
|  - Buffers & sends chunks to API| ----> [ OpenAI Whisper API ]
|  - Stitches & saves transcript. | ----> [ Final Transcript (.json) ]
+---------------------------------+




## Step 1: Foundational Setup & API Proof of Concept
Objective: Create the project structure and confirm that we can successfully communicate with the OpenAI Whisper API. This validates our most critical external dependency before we build anything around it.

Key Tasks:

Create the complete folder structure we designed (without the Docker files).

Set up the .env file with your OpenAI API key.

Write the requirements.txt files with the necessary libraries.

Implement the core/config.py module to securely load the API key.

Create a simple, temporary Python script (test_api.py) that uses the config and a local sample audio file to make a single transcription call to the Whisper API and prints the result.

Checkpoint ✅: We can run python test_api.py from the command line, and it successfully prints the full, accurate transcription of our sample audio file.

## Step 2: Build the Worker (The Processing Engine)
Objective: Create the standalone worker program that will handle all the heavy lifting. At this stage, it will work with local files, not yet connected to the web server.

Key Tasks:

Implement the logic in worker/processor.py to accept an audio file path.

Integrate the Voice Activity Detection (VAD) to read the audio file and filter out silent parts.

Write the "Smart Archiving" logic: save a new, smaller audio file containing only the speech parts.

Implement the "Concurrent Chunking" logic: break the speech-only audio into 2-minute segments and make parallel API calls for each.

Write the logic to stitch the returned transcript segments into a single, coherent text file.

Checkpoint ✅: We can run python worker/main.py /path/to/long_audio.wav. The program will process the file, and we will see two new files created in the uploads directory: a final, silence-trimmed audio file and a complete, stitched JSON transcript.

## Step 3: Build the Web Server & Task Queue
Objective: Create the user-facing server and the communication channel that sends jobs to the Worker.

Key Tasks:

Set up a simple task queue. We'll use Redis for this, as it's lightweight, fast, and easy to set up.

Implement the FastAPI application in web_server/main.py.

Implement the web_server/websocket_manager.py to handle WebSocket connections, receive audio chunks, and save them.

Write the logic that, upon receiving a "stop" signal, sends a single task message to the Redis queue (e.g., {"task": "transcribe", "audio_path": "uploads/audio/session-123.wav"}).

Modify the worker/main.py to listen to the Redis queue for new tasks instead of running on a hardcoded file path.

Checkpoint ✅: We can run both the Web Server and the Worker. We can use a simple test tool to send audio chunks via WebSocket. We'll see the audio file being created on the server, and when we send a "stop" message, a task appears in Redis and the Worker picks it up and processes it.

## Step 4: Develop the Frontend
Objective: Build the user interface that allows a user to record audio and interact with our backend.

Key Tasks:

Create the static/index.html page with Start, Pause/Resume, and Stop buttons.

Write the JavaScript in static/js/main.js to:

Handle the button states (e.g., disable "Start" when recording).

Use the MediaRecorder API to capture microphone audio.

Establish the WebSocket connection to our server.

Send audio chunks and control messages (pause, stop) to the server.

Display status updates ("Recording...", "Processing...", "Done!").

Checkpoint ✅: The webpage works. We can click "Start," and the Web Server logs show it's receiving audio. We can click "Pause" and see the server handle the signal. We can click "Stop," and a task is successfully sent to the Worker via the task queue.

## Step 5: Final Integration & Testing
Objective: Connect all the pieces into a seamless flow and ensure the application is robust.

Key Tasks:

Implement the final confirmation message from the worker back to the user's browser.

Thoroughly test the entire user journey with both short and long recordings.

Test the pause/resume functionality to ensure it doesn't corrupt the audio or transcript.

Add robust error handling (e.g., what happens if an API call fails?).

Checkpoint ✅: The entire application works flawlessly from end to end. A user can record a multi-minute audio session with pauses, and a few moments after clicking "Stop," the final files are correctly saved on the server, and the user gets a success message.

This structured plan will guide us through the development process logically and ensure we have a solid, testable application at every stage. Once you're ready, we can begin with Step 1.

