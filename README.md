# How to run the app

## Prerequisites
- [uv](https://docs.astral.sh/uv/) — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Docker + Docker Compose

---

## Local development

### 1. Environment
```bash
cp .env.example .env
# fill in OPENAI_API_KEY in .env — everything else is pre-filled for local dev
```

### 2. Fix uploads directory (first time only)
```bash
sudo mkdir -p uploads/temp && sudo chown -R $USER uploads/
```

### 3. Install dependencies
```bash
uv sync
```

### 4. Start local infrastructure (MinIO + DynamoDB Local + Redis)
```bash
docker-compose -f docker-compose.dev.yml up -d
```
This starts:
- **MinIO** (S3 emulator) — API on `localhost:9000`, console on `localhost:9001` (`minioadmin` / `minioadmin`)
- **DynamoDB Local** — `localhost:8001`
- **Redis** — `localhost:6379`

The `minio-init` and `dynamodb-init` containers run once to create the `recordings` bucket and `sessionIndex` table, then exit.

### 5. Run the web server (terminal 1)
```bash
uv run uvicorn web_server.main:app --reload --port 8000
```

### 6. Run the worker (terminal 2)
```bash
uv run python -m worker.main
```

### 7. Verify
```bash
curl http://localhost:8000/health/detailed
# {"status":"healthy","services":{"redis":"healthy","s3":"healthy"}}
```

### Teardown
```bash
docker-compose -f docker-compose.dev.yml down       # stop services
docker-compose -f docker-compose.dev.yml down -v    # stop + wipe volumes (fresh start)
```

---

## Adding / updating dependencies
```bash
uv add <package>          # add a new dep
uv remove <package>       # remove a dep
uv sync                   # sync venv to lockfile (run after pulling changes)
```

---


# Proper Documantation of the app 

## 1. ```core ```/
- The **core** directory is the central, shared library of entire application. It contains all the essential code, models, and configurations that are used by both the web_server and the worker.

    A. **auth.py** - This file is responsible for authentication of the web app any request that ia comming its checks for JWT token then validates the exp. time and then if thats valid moves forward 

    B. **config.py** - This file is responsible for loading the .env in side a calss setting and then for all the places in app we will use this to get the .env file value 

    C. **logging_config.py** - This file is config for logging of the app

    D. **models.py** - This file is to validate imput

    E. **services.py** - This is responsible for summery this files calls OpenAI API on transcription.

## 2. ```web_server```/ 
This is the main entry pont of web app we are managing the websocket and all API fron here.

A. **```main.py```** This is the entry point of the web app where we have defined all the API and web-socket connection and defining CORS and adding the security moddlewere. It contains a lifespan manager that runs when the server starts and stops. On startup, it establishes and verifies the connection to your Azure Queue, Blob, and Table services. On shutdown, it gracefully closes these connections. This is a robust pattern for managing external resources.

   ### API Routing: It defines all the user-facing endpoints:
   - GET /: Serves your main index.html frontend(for now).
   - GET /health & GET /health/detailed: Provides health check endpoints, which are essential for monitoring your          application in production.
   - POST /upload: Handles the simple, synchronous file upload workflow. It takes a file and metadata, passes it to the upload_handler, and returns the result.
   - GET /results/{session_id}: Provides the polling endpoint for the frontend to check the status and retrieve the summary of a completed live recording.
   - WebSocket /ws/{session_id}: This is the endpoint for live, real-time audio streaming. It accepts the WebSocket connection and passes it off to the websocket_manager to be handled.

B. **```websocket_manager.py```** - This file is a specialized "Live Recording Manager". Its only job is to handle the logic for an active WebSocket connection, making it as efficient and scalable as possible.

   ### Key Responsibilities:

- Buffering: It receives a constant stream of tiny audio chunks from the browser. Instead of overwhelming the backend with these, it intelligently collects them in an in-memory buffer.

- Batching: Once the buffer reaches a set size (e.g., 5 seconds of audio), it prepares the batch for the next step.

- Packetizing: Because a large batch of audio is too big for a single queue message, this manager splits the batch into multiple, smaller "packets," each under the 64KB limit.

- Task Creation: It creates a structured TranscriptionTask message for each packet (including sequencing information) and sends them to the Azure Queue to be processed by the worker.

- Graceful Finalization: When the user stops recording, it ensures any leftover audio in the buffer is sent, and then it sends a final "finalize" task to the queue to signal the end of the session.


C. **``` storage_handler.py```** - This file is a dedicated "Data Retriever". Its only job is to securely fetch completed results from your cloud storage.

   ### Key Responsibilities:-

   - Index Lookup: When asked for a session_id, it first performs a fast query on your "Master Index" in Azure Table Storage to find the file's location.
   
   - File Retrieval: It then uses the path from the index to connect to Azure Blob Storage, download the correct final transcript JSON file.

   - Data Extraction: It parses the JSON and returns only the necessary data (the status and the summary) to the main API endpoint. This keeps the API response lightweight and efficient.



## 3. ```Worker```/ 
The worker is a long-running, scalable service that listens for jobs on the Azure Queue. Its sole purpose is to take the audio data sent by the web_server, process it through a multi-stage pipeline (VAD, transcription, summarization), and store the final results securely in Azure Storage.

A. **``` main.py```** -This is the main entry point and orchestrator for the entire worker process. It manages the worker's lifecycle, from startup to graceful shutdown, and delegates tasks to the appropriate components.

   ### Key Responsibilities:-

   - Worker Class: Encapsulates all the top-level logic for the service.

    - setup(): This is the critical startup routine. It initializes all the necessary clients for connecting to Azure (Queues, Blobs, and Tables) and pre-loads the VAD model into memory to be shared by all sessions. It also creates and starts the SessionManager.

    - shutdown(): Handles a graceful shutdown. It stops accepting new work, ensures any in-progress tasks are finished, and cleanly closes all connections to Azure.

    - listen_for_tasks(): This is the core infinite loop of the worker. It efficiently polls the Azure Queue for new messages in batches, using an exponential backoff to save resources when the queue is empty.

    - handle_message(): This function acts as the delegator. When it receives a task, it doesn't do the work itself. It uses the SessionManager to find or create the correct SessionProcessor and then tells that processor what to do (e.g., "process this chunk" or "finalize this session"). It also contains the robust logic for handling failed tasks by moving them to the Dead-Letter Queue (DLQ).


B. **``` session_manager.py```** - This is a specialized component that is the single source of truth for managing the lifecycle of all active user sessions. Its primary role is to ensure stability and, most importantly, prevent memory leaks.

   ### Key Responsibilities:-

   - ession Tracking: It maintains an internal dictionary of all active SessionProcessor objects, keyed by their session_id.

    - get_or_create_session(): A factory method that intelligently finds an existing session or creates a brand new SessionProcessor instance when the first task for a new session arrives.

    - finalize_and_remove_session(): This is the explicit cleanup function. It calls the processor's finalize_session method to finish the work and then removes the processor object from its tracking dictionary, allowing Python's garbage collector to free up all the memory associated with that session.

    - cleanup_stale_sessions() (The "Janitor"): This is the critical memory leak prevention feature. It's a background task that runs periodically (e.g., every 10 minutes) to find and automatically clean up any "orphaned" sessions that have been inactive for too long (e.g., a user's browser crashed)


C. **``` processor.py```** - This is where the actual, detailed work of processing a single session's audio happens. An instance of the SessionProcessor class is created for each active recording.

   ### Key Responsibilities:-

   - Packet Reassembly (reassemble_and_process_chunk): It receives the small "packets" sent by the web_server and intelligently puts them back together into a complete audio chunk before processing.

    - Audio Processing (process_chunk): This is the core logic. It decodes the audio, handles potential WAV headers, and runs the audio through the VAD model (with a robust fallback if the VAD fails) to filter out silence.

    - Non-Blocking Buffering: It appends all speech-only audio to two highly efficient deque buffers: one for the final audio file (final_audio_buffer) and one for the current transcription batch (transcription_buffer). All slow, CPU-intensive operations (like data conversion and file writing) are correctly offloaded to a background thread pool to keep the application responsive.

    - Concurrent Transcription (_trigger_transcription_task): When the transcription buffer is full, it creates an in-memory .wav file and sends it to the Whisper API in a background task, allowing it to continue processing new audio while waiting for the result.

    - Finalization (finalize_session): At the end of a session, this function orchestrates the final steps: it stitches together all the partial transcripts, calls the shared service to generate a summary, uploads the final audio and transcript files to Azure Blob Storage, and creates the "Master Index" entry in Azure Table Storage.

    - Resource Cleanup (_cleanup_resources): A final, explicit cleanup method that is called at the very end of finalization to clear all internal buffers and lists, ensuring all memory for the session is released.




{
   overall summery:

   alergies: if not null we have to choose from a category list 

   disies with ICD code: if not null

   cronic condition:  if now null

   vitals:  for example pulse, tempratue, BP , O2sat , etcarta 

   family and social history: in paragraph

   past medical history: 

   observation and physical examination : 

   treatment plan : 
   
}

