// Tauri shell.
//
// Its only job is process lifecycle: pick a free port, start the frozen
// backend, wait until it answers, then show the window. On exit, kill the
// backend and any llama-server processes it started — otherwise model
// weights stay resident in RAM after the app closes, and the next launch
// fails to bind its ports.
//
// The backend is spawned with std::process::Command rather than through
// tauri-plugin-shell. That plugin exists so the *frontend* can spawn
// processes, which means an allowlist and a capabilities scope; we only ever
// spawn from Rust, so that layer is cost without benefit — and a
// misconfigured scope fails silently, which is a bad trade for something
// this load-bearing.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader};
use std::net::TcpListener;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{Manager, State};

struct Backend {
    child: Mutex<Option<Child>>,
    port: u16,
}

/// Bind port 0 and let the OS choose. Avoids failing when something else
/// already holds the port we would have hardcoded.
fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .unwrap_or(8000)
}

/// Sidecars are installed beside the shell executable.
fn sidecar_path(name: &str) -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let dir = exe.parent()?.to_path_buf();

    let candidates = [
        dir.join(format!("{name}.exe")),
        dir.join(name),
        // Development: cargo tauri dev leaves the binaries where the build
        // script put them.
        dir.join("../../../binaries").join(format!("{name}.exe")),
    ];
    candidates.into_iter().find(|p| p.exists())
}

/// The frontend asks for this on startup so its fetch calls know where to go.
#[tauri::command]
fn backend_port(state: State<Backend>) -> u16 {
    state.port
}

fn wait_ready(port: u16, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if std::net::TcpStream::connect(("127.0.0.1", port)).is_ok() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(200));
    }
    false
}

fn main() {
    let port = free_port();

    tauri::Builder::default()
        .manage(Backend {
            child: Mutex::new(None),
            port,
        })
        .invoke_handler(tauri::generate_handler![backend_port])
        .setup(move |app| {
            let exe = match sidecar_path("workbench-backend") {
                Some(p) => p,
                None => {
                    eprintln!("workbench-backend not found next to the shell executable");
                    if let Some(w) = app.get_webview_window("main") {
                        let _ = w.show();
                    }
                    return Ok(());
                }
            };

            eprintln!("starting backend: {} --port {}", exe.display(), port);

            let mut child = Command::new(&exe)
                .args(["--port", &port.to_string()])
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .spawn()?;

            // Backend output goes to the terminal. Without this a startup
            // failure is completely silent, which is how this bug hid.
            if let Some(out) = child.stdout.take() {
                std::thread::spawn(move || {
                    for line in BufReader::new(out).lines().map_while(Result::ok) {
                        println!("[backend] {line}");
                    }
                });
            }
            if let Some(err) = child.stderr.take() {
                std::thread::spawn(move || {
                    for line in BufReader::new(err).lines().map_while(Result::ok) {
                        eprintln!("[backend] {line}");
                    }
                });
            }

            app.state::<Backend>().child.lock().unwrap().replace(child);

            // A one-file PyInstaller build unpacks to a temp directory on
            // launch, which is slow the first time and after any cache
            // eviction. This ceiling is generous on purpose.
            if wait_ready(port, Duration::from_secs(120)) {
                eprintln!("backend ready on port {port}");
            } else {
                eprintln!("backend did not answer on port {port} within 120s");
            }

            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                let state = window.state::<Backend>();
                if let Some(mut child) = state.child.lock().unwrap().take() {
                    let _ = child.kill();
                    let _ = child.wait();
                }
                // The backend detaches llama-server per loaded model so those
                // survive a backend restart, which means they need killing
                // explicitly here or model weights stay resident in memory and
                // the next launch cannot bind its ports.
                let _ = Command::new("taskkill")
                    .args(["/F", "/IM", "llama-server.exe"])
                    .status();
            }
        })
        .run(tauri::generate_context!())
        .expect("failed to start the workbench shell");
}
