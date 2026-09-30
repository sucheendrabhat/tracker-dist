# GATE CSE Tracker — Distribution Package

This standalone package contains everything needed to install, run, and host the **GATE CSE Tracker** on Linux, macOS, or Windows.

---

## 📁 Package Structure

```
dist/
├── tracker_app/               <-- Python Desktop Tracker Source Code
│   ├── app.py                 <-- Main GUI script
│   ├── database.py            <-- SQLite storage layer
│   ├── indexer.py             <-- PDF parsing & snapshot engine
│   ├── export.py              <-- PDF weak-spot export engine
│   ├── stats_export.py        <-- JSON exporter for web dashboard
│   ├── sync.py                <-- Background Git auto-sync module
│   └── requirements.txt       <-- Python package dependencies
├── website/                   <-- Web Dashboard Source Code
│   ├── index.html             <-- Web app template
│   ├── style.css              <-- Modern CSS styling
│   ├── app.js                 <-- JavaScript charts & dashboard logic
│   └── data/
│       └── stats.json         <-- Sample / Exported statistics JSON
├── install.sh                 <-- Automated setup script (Linux / macOS)
├── run.sh                     <-- Quick launcher (Linux / macOS)
├── install.bat                <-- Automated setup script (Windows)
├── run.bat                    <-- Quick launcher (Windows)
└── README.md                  <-- Setup & user manual (This file)
```

---

## 🚀 Quick Start (Installation & Launch)

### Linux & macOS

1. Open your terminal in this folder and run the installer:
   ```bash
   ./install.sh
   ```
2. Launch the desktop tracker application:
   ```bash
   ./run.sh
   ```

> **Note for Linux Users**: If you get a Tkinter warning, install `python3-tk`:
> - Debian/Ubuntu/Mint: `sudo apt install python3-tk`
> - Fedora: `sudo dnf install python3-tkinter`

### Windows

1. Double-click **`install.bat`** (creates the virtual environment and installs dependencies).
2. Double-click **`run.bat`** to launch the tracker application.

---

## 📖 How to Use the Desktop Tracker

1. **Load GATE Overflow PDFs**:
   - On first launch, click **"Load Volume(s)..."**.
   - Multi-select your 3 Volume GATE Overflow CSE PDFs.
   - The app parses question numbers, answer keys, chapter links, and diagram bounds (~10s per volume). Future launches are instant.

2. **Search & Track Questions**:
   - Type any question ID (e.g., `3.6.27`) or click through subjects.
   - Rate difficulty:
     - 🟩 **L1 (Easy)**: Understood and solved smoothly.
     - 🟧 **L2 (Forgot)**: Forgot a formula, trick, or technique.
     - 🟥 **L3 (Didn't Understand)**: Need detailed revision.
   - Type notes in the text box — **autosaves automatically** as you type.

3. **Analytics & Heatmap**:
   - Check the **Dashboard**, **Analytics**, and **Activity** tabs to view your daily question count, 18-week study heatmap, and streak metrics.

4. **Export Weak Spots to PDF**:
   - Click **File → Export Weak Spots (PDF)**.
   - Select filters (e.g. L3 questions only).
   - Generates a PDF revision deck with high-resolution cropped diagram/formula snapshot images straight from the original PDFs along with your notes.

---

## 🌐 Setting Up Optional Live Web Dashboard (GitHub Pages)

Want your preparation stats accessible from any device or mobile browser?

### Step 1: Create a GitHub Repository
1. Create a public repository on GitHub named **`gate-track`**.
2. Copy the contents of the `website/` folder into your `gate-track` local repository folder.
3. Push to GitHub:
   ```bash
   git init
   git remote add origin https://github.com/YOUR_USERNAME/gate-track.git
   git branch -M main
   git add .
   git commit -m "Deploy GATE tracker website"
   git push -u origin main
   ```
4. Enable GitHub Pages in your repo settings (**Settings → Pages → Source: main branch / root folder**).

### Step 2: Enable Auto-Sync in Desktop App
1. Open the desktop app (`./run.sh` or `run.bat`).
2. Go to **Website → Set Website Repo Folder...**.
3. Select your local `gate-track` repository directory.

*Whenever you study and rate questions, the app will automatically update `data/stats.json`, commit, and push to GitHub Pages in the background!*

---

## 🛠️ Testing Web Dashboard Locally

To test the website locally on your computer:
```bash
cd website
python3 -m http.server 8000
```
Then visit `http://localhost:8000` in your web browser.
