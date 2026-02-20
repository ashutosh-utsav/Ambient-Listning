// document.addEventListener("DOMContentLoaded", () => {
//     const startButton = document.getElementById("startButton");
//     const pauseButton = document.getElementById("pauseButton");
//     const resumeButton = document.getElementById("resumeButton");
//     const stopButton = document.getElementById("stopButton");
//     const statusElement = document.getElementById("status");

//     let mediaRecorder;
//     let websocket;
//     let sessionId;

//     const setupMediaRecorder = (stream) => {
//         mediaRecorder = new MediaRecorder(stream);
        
//         mediaRecorder.ondataavailable = (event) => {
//             if (event.data.size > 0 && websocket && websocket.readyState === WebSocket.OPEN) {
//                 websocket.send(event.data);
//             }
//         };

//         mediaRecorder.onstart = () => {
//             statusElement.textContent = "Recording...";
//             startButton.style.display = 'none';
//             pauseButton.style.display = 'inline-block';
//             resumeButton.style.display = 'none';
//             stopButton.style.display = 'inline-block';
//         };

//         mediaRecorder.onpause = () => {
//             statusElement.textContent = "Paused.";
//             pauseButton.style.display = 'none';
//             resumeButton.style.display = 'inline-block';
//         };

//         mediaRecorder.onresume = () => {
//             statusElement.textContent = "Recording...";
//             pauseButton.style.display = 'inline-block';
//             resumeButton.style.display = 'none';
//         };

//         mediaRecorder.onstop = () => {
//             statusElement.textContent = "Recording finished. Processing on server...";
//             startButton.style.display = 'inline-block';
//             pauseButton.style.display = 'none';
//             resumeButton.style.display = 'none';
//             stopButton.style.display = 'none';
            
//             if (websocket && websocket.readyState === WebSocket.OPEN) {
//                 websocket.close();
//             }
//         };
//     };

//     const startRecording = async () => {
//         sessionId = `session_${Date.now()}`;
//         const wsUrl = `ws://localhost:8000/ws/${sessionId}`;
//         websocket = new WebSocket(wsUrl);

//         websocket.onopen = async () => {
//             console.log("WebSocket connection established.");
//             try {
//                 const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
//                 setupMediaRecorder(stream);
//                 mediaRecorder.start(1000); // Send data every 1000ms (1 second)
//             } catch (error) {
//                 console.error("Error accessing microphone:", error);
//                 statusElement.textContent = "Could not access microphone. Please grant permission.";
//                 websocket.close();
//             }
//         };

//         websocket.onerror = (error) => {
//             console.error("WebSocket error:", error);
//             statusElement.textContent = "Error: Could not connect to server.";
//         };
        
//         websocket.onclose = () => {
//             console.log("WebSocket connection closed.");
//             // You can add logic here to notify the user that processing is complete
//             // For now, we just log it.
//         };
//     };

//     const pauseRecording = () => {
//         if (mediaRecorder && mediaRecorder.state === "recording") {
//             mediaRecorder.pause();
//         }
//     };

//     const resumeRecording = () => {
//         if (mediaRecorder && mediaRecorder.state === "paused") {
//             mediaRecorder.resume();
//         }
//     };

//     const stopRecording = () => {
//         if (mediaRecorder && mediaRecorder.state !== "inactive") {
//             mediaRecorder.stop();
//         }
//     };

//     startButton.addEventListener("click", startRecording);
//     pauseButton.addEventListener("click", pauseRecording);
//     resumeButton.addEventListener("click", resumeRecording);
//     stopButton.addEventListener("click", stopRecording);
// });