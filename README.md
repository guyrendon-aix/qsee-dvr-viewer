# Q-See 8-Channel DVR Web & Desktop Viewer

A modern, high-performance cross-platform desktop and web monitoring application built to revive legacy **Q-See 8-channel analog DVRs** (Dahua OEM, Firmware version `2.616.QS01.1`). 

Because official manufacturer support for these legacy devices has ended, web plugins no longer function in modern browsers, and proprietary client applications are defunct, this application bridges the gap—providing a secure, lightning-fast web dashboard and native desktop executables.

---

## 🚀 Key Features

* **Real-Time RTSP Pipeline:** Optimized OpenCV and FFmpeg backend worker threads enforcing TCP transport and frame buffer minimization (`CAP_PROP_BUFFERSIZE = 1`) to eliminate startup lag.
* **Dual-Stream Architecture:** Automatically utilizes low-resolution sub-streams (`subtype=1`) for efficient multi-camera grid viewing and instantly switches to HD main-streams (`subtype=0`) when expanding a camera to maximum fullscreen with a clean minimize button.
* **Responsive Smart Grid:** Automatically scales, rearranges, and flows tiles based on your browser viewport size, with the ability to toggle individual cameras on or off from the settings panel.
* **Secure Authentication & User Management:** Built-in session security with default `admin:admin` credentials, allowing administrators to update passwords and manage additional users via the settings interface.
* **Cross-Platform Standalone Executables:** Packaged effortlessly via PyInstaller and automated GitHub Actions into click-to-run binaries for **Windows**, **macOS**, and **Linux**.

---

## 🛠️ Tech Stack

* **Backend:** Python, FastAPI, Uvicorn, OpenCV, NumPy, Pydantic, Passlib
* **Frontend:** Tailwind CSS (Dark Mode), HTML5, JavaScript (Fetch / MJPEG streaming)
* **Build / CI/CD:** PyInstaller, GitHub Actions

---

## 📡 RTSP Stream Structure

The application communicates with the Q-See DVR using standard Dahua-OEM RTSP endpoints:
```text
rtsp://[username]:[password]@[dvr_ip]:554/cam/realmonitor?channel=[1-8]&subtype=[0|1]
```
* `subtype=1`: Sub-stream (optimized for low-bandwidth multi-grid display)
* `subtype=0`: Main-stream (crystal-clear HD single/fullscreen view)

---

## ⚙️ Local Installation & Development

If you prefer running or developing the application locally from source:

1. **Clone the Repository:**
   ```bash
   git clone https://github.com/guyrendon-aix/qsee-dvr-viewer.git
   cd qsee-dvr-viewer
   ```

2. **Create and Activate a Virtual Environment:**
   ```bash
   python -m venv venv
   # On Windows PowerShell:
   .\venv\Scripts\Activate
   # On macOS/Linux:
   source venv/bin/activate
   ```

3. **Install Dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Run the Application:**
   ```bash
   python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
   ```
   Open your browser and navigate to `http://127.0.0.1:8000`. Log in using the default credentials (`admin` / `admin`).

5. **Run 24/7 with the Guardian Watchdog (optional):**
   `run_guard.py` starts the server on `0.0.0.0:8000` (reachable from your LAN) and automatically restarts it 3 seconds after any crash or unexpected exit.
   ```bash
   # On Windows PowerShell:
   .\venv\Scripts\python.exe run_guard.py
   # On macOS/Linux:
   venv/bin/python run_guard.py
   ```
   Restart events are timestamped in the console and in `run_guard.log`. Press `Ctrl+C` to stop the guard and the server cleanly.

   The guard doesn't poll. It sleeps until the server exits, then waits `GUARD_RESTART_DELAY` seconds (default `3`, allowed `1`–`300`) before restarting. If the server keeps failing right after starting, the wait doubles each time up to 60 seconds, and resets once the server stays up. To change the delay, copy `.env.example` to `.env` and set it there, or set it as an environment variable, which takes precedence:
   ```bash
   GUARD_RESTART_DELAY=5
   ``` Make sure nothing else is already using port 8000. LAN mode is for trusted networks only, so don't forward the port to the internet.

---

## 📦 Automated Desktop Builds (GitHub Actions)

This repository includes a multi-platform matrix workflow (`.github/workflows/build.yml`) that compiles the app into standalone desktop binaries automatically whenever changes are pushed to GitHub.

1. Go to the **Actions** tab on your GitHub repository (`guyrendon-aix/qsee-dvr-viewer`).
2. Select the latest workflow run.
3. Download the build artifact matching your operating system:
   * `QSeeDVRViewer-Windows.exe`
   * `QSeeDVRViewer-macOS`
   * `QSeeDVRViewer-Linux`