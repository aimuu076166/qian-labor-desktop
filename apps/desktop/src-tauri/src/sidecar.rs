use std::ffi::OsStr;
use std::io::{Read, Write};
use std::net::{Ipv4Addr, SocketAddrV4, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Duration;

use anyhow::{anyhow, Context, Result};
use rand::RngCore;
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager, State};

use crate::credentials::{provider_environment, LocalSecretStore};
use crate::process_ownership::OwnedSidecarProcess;

const READY_FILE_PREFIX: &str = ".qian-sidecar-ready-";
const LOOPBACK_HOST: &str = "127.0.0.1";
const SMOKE_FLAG: &str = "QIAN_RC_SMOKE";
const SMOKE_ROOT: &str = "QIAN_RC_SMOKE_DIR";
const SMOKE_PREFIX: &str = "qian-rc-smoke-";
const SIDECAR_READY_TIMEOUT: Duration = Duration::from_secs(45);

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SidecarReady {
    pub host: String,
    pub port: u16,
    pub pid: u32,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DesktopBackendInfo {
    pub base_url: String,
    pub token: String,
}

pub struct BackendProcess {
    info: DesktopBackendInfo,
    owned_process: OwnedSidecarProcess,
    data_dir: PathBuf,
    sidecar_pid: u32,
    sidecar_port: u16,
    smoke_root: Option<PathBuf>,
}

#[derive(Debug, Clone)]
pub struct PackagedSmokeContext {
    root: PathBuf,
    data_dir: PathBuf,
    sidecar_pid: u32,
}

impl BackendProcess {
    pub fn packaged_smoke_context(&self) -> Option<PackagedSmokeContext> {
        self.smoke_root.as_ref().map(|root| PackagedSmokeContext {
            root: root.clone(),
            data_dir: self.data_dir.clone(),
            sidecar_pid: self.sidecar_pid,
        })
    }
}

pub(crate) trait StoppableBackend {
    fn stop_backend(&mut self) -> Result<(), String>;
}

pub struct BackendState<P = BackendProcess> {
    process: Mutex<Option<P>>,
}

impl<P> Default for BackendState<P> {
    fn default() -> Self {
        Self {
            process: Mutex::new(None),
        }
    }
}

impl<P: StoppableBackend> BackendState<P> {
    pub fn install(&self, process: P) -> Result<(), String> {
        let mut slot = self
            .process
            .lock()
            .map_err(|_| "DESKTOP_BACKEND_STATE_POISONED".to_string())?;
        if slot.is_some() {
            return Err("DESKTOP_BACKEND_ALREADY_RUNNING".to_string());
        }
        *slot = Some(process);
        Ok(())
    }

    pub fn stop(&self) -> Result<(), String> {
        let process = self
            .process
            .lock()
            .map_err(|_| "DESKTOP_BACKEND_STATE_POISONED".to_string())?
            .take();
        if let Some(mut process) = process {
            process.stop_backend()?;
        }
        Ok(())
    }
}

impl BackendState<BackendProcess> {
    fn info(&self) -> Result<DesktopBackendInfo, String> {
        let slot = self
            .process
            .lock()
            .map_err(|_| "DESKTOP_BACKEND_STATE_POISONED".to_string())?;
        slot.as_ref()
            .map(|process| process.info.clone())
            .ok_or_else(|| "DESKTOP_BACKEND_NOT_READY".to_string())
    }
}

trait OwnedProcessControl {
    fn sidecar_exited(&mut self) -> Result<bool, String>;
    fn wait_for_sidecar_exit(&mut self, timeout: Duration) -> Result<bool, String>;
    fn finalize_after_graceful(&mut self) -> Result<(), String>;
    fn force_cleanup(&mut self) -> Result<(), String>;
}

impl OwnedProcessControl for OwnedSidecarProcess {
    fn sidecar_exited(&mut self) -> Result<bool, String> {
        self.sidecar_exited()
    }

    fn wait_for_sidecar_exit(&mut self, timeout: Duration) -> Result<bool, String> {
        self.wait_for_sidecar_exit(timeout)
    }

    fn finalize_after_graceful(&mut self) -> Result<(), String> {
        self.finalize_after_graceful()
    }

    fn force_cleanup(&mut self) -> Result<(), String> {
        self.force_cleanup()
    }
}

fn shutdown_owned_process<P, F>(process: &mut P, graceful_shutdown: F) -> Result<(), String>
where
    P: OwnedProcessControl,
    F: FnOnce() -> Result<(), String>,
{
    if matches!(process.sidecar_exited(), Ok(true)) {
        return process
            .finalize_after_graceful()
            .or_else(|_| process.force_cleanup())
            .map_err(|_| "DESKTOP_BACKEND_STOP_FAILED".to_string());
    }

    let graceful_succeeded = graceful_shutdown().is_ok();
    let graceful_exit = matches!(
        process.wait_for_sidecar_exit(Duration::from_secs(5)),
        Ok(true)
    );
    if graceful_succeeded && graceful_exit && process.finalize_after_graceful().is_ok() {
        return Ok(());
    }

    process
        .force_cleanup()
        .map_err(|_| "DESKTOP_BACKEND_STOP_FAILED".to_string())
}

fn owned_startup_failure<P: OwnedProcessControl>(process: &mut P, code: &str) -> anyhow::Error {
    match process.force_cleanup() {
        Ok(()) => anyhow::Error::msg(code.to_string()),
        Err(_) => anyhow!("DESKTOP_SIDECAR_STARTUP_CLEANUP_FAILED"),
    }
}

fn request_graceful_shutdown(port: u16, token: &str) -> Result<(), String> {
    let address = SocketAddrV4::new(Ipv4Addr::LOCALHOST, port);
    let mut stream = TcpStream::connect_timeout(&address.into(), Duration::from_secs(2))
        .map_err(|_| "DESKTOP_GRACEFUL_SHUTDOWN_FAILED".to_string())?;
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .map_err(|_| "DESKTOP_GRACEFUL_SHUTDOWN_FAILED".to_string())?;
    stream
        .set_write_timeout(Some(Duration::from_secs(2)))
        .map_err(|_| "DESKTOP_GRACEFUL_SHUTDOWN_FAILED".to_string())?;
    let request = format!(
        "POST /api/internal/shutdown HTTP/1.1\r\nHost: {LOOPBACK_HOST}:{port}\r\nX-Qian-Desktop-Token: {token}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    );
    stream
        .write_all(request.as_bytes())
        .map_err(|_| "DESKTOP_GRACEFUL_SHUTDOWN_FAILED".to_string())?;
    let mut response = [0_u8; 256];
    let count = stream
        .read(&mut response)
        .map_err(|_| "DESKTOP_GRACEFUL_SHUTDOWN_FAILED".to_string())?;
    let status_line = std::str::from_utf8(&response[..count])
        .ok()
        .and_then(|value| value.lines().next())
        .unwrap_or_default();
    if status_line.starts_with("HTTP/1.1 202 ") || status_line.starts_with("HTTP/1.0 202 ") {
        Ok(())
    } else {
        Err("DESKTOP_GRACEFUL_SHUTDOWN_FAILED".to_string())
    }
}

impl StoppableBackend for BackendProcess {
    fn stop_backend(&mut self) -> Result<(), String> {
        let port = self.sidecar_port;
        let token = self.info.token.clone();
        shutdown_owned_process(&mut self.owned_process, || {
            request_graceful_shutdown(port, &token)
        })
    }
}

fn parse_ready_payload(payload: &[u8]) -> Result<SidecarReady, String> {
    let ready: SidecarReady =
        serde_json::from_slice(payload).map_err(|_| "DESKTOP_READY_JSON_INVALID".to_string())?;
    if ready.host != LOOPBACK_HOST {
        return Err("DESKTOP_READY_HOST_INVALID".to_string());
    }
    if ready.port == 0 {
        return Err("DESKTOP_READY_PORT_INVALID".to_string());
    }
    if ready.pid == 0 {
        return Err("DESKTOP_READY_PID_INVALID".to_string());
    }
    Ok(ready)
}

fn random_launch_token() -> String {
    let mut bytes = [0_u8; 32];
    rand::rng().fill_bytes(&mut bytes);
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn ready_file_path(data_dir: &Path) -> PathBuf {
    let mut bytes = [0_u8; 16];
    rand::rng().fill_bytes(&mut bytes);
    let nonce: String = bytes.iter().map(|byte| format!("{byte:02x}")).collect();
    data_dir.join(format!("{READY_FILE_PREFIX}{nonce}.json"))
}

fn ready_temporary_path(ready_file: &Path) -> PathBuf {
    let mut name = ready_file.as_os_str().to_os_string();
    name.push(".tmp");
    PathBuf::from(name)
}

fn cleanup_ready_files(ready_file: &Path) {
    let _ = std::fs::remove_file(ready_file);
    let _ = std::fs::remove_file(ready_temporary_path(ready_file));
}

fn read_ready_file(ready_file: &Path) -> Result<Option<Vec<u8>>, String> {
    let metadata = match std::fs::symlink_metadata(ready_file) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err("DESKTOP_READY_FILE_UNAVAILABLE".to_string()),
    };
    if !metadata.is_file() || metadata.file_type().is_symlink() || metadata.len() > 1024 {
        return Err("DESKTOP_READY_FILE_INVALID".to_string());
    }
    std::fs::read(ready_file)
        .map(Some)
        .map_err(|_| "DESKTOP_READY_FILE_UNAVAILABLE".to_string())
}

fn bundled_sidecar_path(executable: &Path) -> Result<PathBuf, String> {
    let directory = executable
        .parent()
        .ok_or_else(|| "DESKTOP_EXECUTABLE_DIR_UNAVAILABLE".to_string())?;
    #[cfg(windows)]
    let name = "qian-sidecar.exe";
    #[cfg(not(windows))]
    let name = "qian-sidecar";
    Ok(directory.join(name))
}

fn runtime_smoke_root(requested: &Path, canonical: &Path, windows: bool) -> PathBuf {
    if windows {
        requested.to_path_buf()
    } else {
        canonical.to_path_buf()
    }
}

fn validated_smoke_root(flag: Option<&OsStr>, root: Option<&OsStr>) -> Result<PathBuf, String> {
    if flag != Some(OsStr::new("1")) {
        return Err("RC_SMOKE_FLAG_INVALID".to_string());
    }
    let requested = root.ok_or_else(|| "RC_SMOKE_ROOT_REQUIRED".to_string())?;
    let requested_path = Path::new(requested);
    if !requested_path.is_absolute() {
        return Err("RC_SMOKE_ROOT_INVALID".to_string());
    }
    let metadata = std::fs::symlink_metadata(requested_path)
        .map_err(|_| "RC_SMOKE_ROOT_UNAVAILABLE".to_string())?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Err("RC_SMOKE_ROOT_INVALID".to_string());
    }
    let canonical_temp = std::env::temp_dir()
        .canonicalize()
        .map_err(|_| "RC_SMOKE_TEMP_UNAVAILABLE".to_string())?;
    let canonical_root = requested_path
        .canonicalize()
        .map_err(|_| "RC_SMOKE_ROOT_UNAVAILABLE".to_string())?;
    if !canonical_root.starts_with(&canonical_temp) || canonical_root == canonical_temp {
        return Err("RC_SMOKE_ROOT_OUTSIDE_TEMP".to_string());
    }
    let basename = canonical_root
        .file_name()
        .and_then(OsStr::to_str)
        .ok_or_else(|| "RC_SMOKE_ROOT_INVALID".to_string())?;
    if !basename.starts_with(SMOKE_PREFIX) {
        return Err("RC_SMOKE_ROOT_PREFIX_INVALID".to_string());
    }
    // std::fs::canonicalize produces a verbatim `\\?\` path on Windows.
    // Python's SQLite stack receives the already-validated absolute runner
    // path instead, while other platforms retain the canonical path.
    Ok(runtime_smoke_root(
        requested_path,
        &canonical_root,
        cfg!(windows),
    ))
}

fn configured_smoke_root() -> Result<Option<PathBuf>, String> {
    let flag = std::env::var_os(SMOKE_FLAG);
    if flag.is_none() {
        return Ok(None);
    }
    validated_smoke_root(flag.as_deref(), std::env::var_os(SMOKE_ROOT).as_deref()).map(Some)
}

fn stable_failure_code(code: &str) -> bool {
    let bytes = code.as_bytes();
    (3..=160).contains(&bytes.len())
        && bytes[0].is_ascii_uppercase()
        && bytes[1..]
            .iter()
            .all(|byte| byte.is_ascii_uppercase() || byte.is_ascii_digit() || *byte == b'_')
}

pub fn record_packaged_smoke_failure(error: &anyhow::Error) {
    let code = error.to_string();
    if !stable_failure_code(&code) {
        return;
    }
    let Ok(Some(root)) = configured_smoke_root() else {
        return;
    };
    let Ok(payload) = serde_json::to_vec(&serde_json::json!({"code": code})) else {
        return;
    };
    let temporary = root.join("failure.json.tmp");
    let destination = root.join("failure.json");
    if std::fs::write(&temporary, payload).is_ok() {
        let _ = std::fs::rename(temporary, destination);
    }
}

pub async fn start_backend(app: AppHandle) -> Result<BackendProcess> {
    let smoke_root = configured_smoke_root().map_err(anyhow::Error::msg)?;
    let data_dir = match &smoke_root {
        Some(root) => root.join("app-data"),
        None => app
            .path()
            .app_data_dir()
            .context("DESKTOP_APP_DATA_DIR_UNAVAILABLE")?,
    };
    std::fs::create_dir_all(&data_dir).context("DESKTOP_APP_DATA_DIR_CREATE_FAILED")?;
    let provider_env = provider_environment(&LocalSecretStore::new(&data_dir), &data_dir)
        .map_err(anyhow::Error::msg)
        .context("DESKTOP_PROVIDER_SESSION_FAILED")?;

    let token = random_launch_token();
    let executable = std::env::current_exe().context("DESKTOP_EXECUTABLE_PATH_UNAVAILABLE")?;
    let sidecar = bundled_sidecar_path(&executable).map_err(anyhow::Error::msg)?;
    let sidecar_directory = sidecar
        .parent()
        .context("DESKTOP_SIDECAR_DIR_UNAVAILABLE")?;
    let ready_file = ready_file_path(&data_dir);
    let ready_temporary = ready_temporary_path(&ready_file);
    for candidate in [&ready_file, &ready_temporary] {
        match std::fs::symlink_metadata(candidate) {
            Ok(_) => return Err(anyhow!("DESKTOP_READY_FILE_ALREADY_EXISTS")),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(_) => return Err(anyhow!("DESKTOP_READY_FILE_UNAVAILABLE")),
        }
    }
    let mut owned_process = OwnedSidecarProcess::spawn_sidecar(
        &sidecar,
        sidecar_directory,
        &data_dir,
        &token,
        &ready_file,
        &provider_env,
    )
    .map_err(anyhow::Error::msg)
    .context("DESKTOP_SIDECAR_SPAWN_FAILED")?;

    let deadline = tokio::time::Instant::now() + SIDECAR_READY_TIMEOUT;
    let ready = loop {
        match read_ready_file(&ready_file) {
            Ok(Some(payload)) => match parse_ready_payload(&payload) {
                Ok(ready) => {
                    cleanup_ready_files(&ready_file);
                    let exited = match owned_process.sidecar_exited() {
                        Ok(exited) => exited,
                        Err(_) => {
                            return Err(owned_startup_failure(
                                &mut owned_process,
                                "DESKTOP_SIDECAR_EVENT_ERROR",
                            ));
                        }
                    };
                    if exited {
                        return Err(owned_startup_failure(
                            &mut owned_process,
                            "DESKTOP_SIDECAR_TERMINATED_BEFORE_READY",
                        ));
                    }
                    break ready;
                }
                Err(error) => {
                    cleanup_ready_files(&ready_file);
                    return Err(owned_startup_failure(&mut owned_process, &error));
                }
            },
            Ok(None) => {}
            Err(error) => {
                cleanup_ready_files(&ready_file);
                return Err(owned_startup_failure(&mut owned_process, &error));
            }
        }

        let exited = match owned_process.sidecar_exited() {
            Ok(exited) => exited,
            Err(_) => {
                return Err(owned_startup_failure(
                    &mut owned_process,
                    "DESKTOP_SIDECAR_EVENT_ERROR",
                ));
            }
        };
        if exited {
            cleanup_ready_files(&ready_file);
            return Err(owned_startup_failure(
                &mut owned_process,
                "DESKTOP_SIDECAR_TERMINATED_BEFORE_READY",
            ));
        }
        if tokio::time::Instant::now() >= deadline {
            cleanup_ready_files(&ready_file);
            return Err(owned_startup_failure(
                &mut owned_process,
                "DESKTOP_SIDECAR_READY_TIMEOUT",
            ));
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    };

    Ok(BackendProcess {
        info: DesktopBackendInfo {
            base_url: format!("http://{}:{}", ready.host, ready.port),
            token,
        },
        owned_process,
        data_dir,
        sidecar_pid: ready.pid,
        sidecar_port: ready.port,
        smoke_root,
    })
}

pub async fn run_packaged_smoke(app: AppHandle, context: PackagedSmokeContext) {
    let started = serde_json::json!({"sidecar_pid": context.sidecar_pid});
    let started_temporary = context.root.join("started.json.tmp");
    let started_destination = context.root.join("started.json");
    let started_written = serde_json::to_vec(&started)
        .map_err(|_| ())
        .and_then(|bytes| std::fs::write(&started_temporary, bytes).map_err(|_| ()))
        .and_then(|_| std::fs::rename(&started_temporary, &started_destination).map_err(|_| ()))
        .is_ok();
    if !started_written {
        record_packaged_smoke_failure(&anyhow!("RC_SMOKE_STARTED_WRITE_FAILED"));
        if let Some(state) = app.try_state::<BackendState>() {
            let _ = state.stop();
        }
        app.exit(1);
        return;
    }

    let database = context.data_dir.join("qian-labor.db");
    let deadline = tokio::time::Instant::now() + Duration::from_secs(15);
    while !database.is_file() && tokio::time::Instant::now() < deadline {
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let database_created = database.is_file();
    let cleanup_complete = app
        .try_state::<BackendState>()
        .is_some_and(|state| state.stop().is_ok());
    if !cleanup_complete {
        record_packaged_smoke_failure(&anyhow!("RC_SMOKE_SIDECAR_CLEANUP_FAILED"));
        app.exit(1);
        return;
    }
    let result = serde_json::json!({
        "database_created": database_created,
        "sidecar_pid": context.sidecar_pid,
        "cleanup_complete": cleanup_complete,
    });
    let temporary = context.root.join("result.json.tmp");
    let destination = context.root.join("result.json");
    let written = serde_json::to_vec(&result)
        .map_err(|_| ())
        .and_then(|bytes| std::fs::write(&temporary, bytes).map_err(|_| ()))
        .and_then(|_| std::fs::rename(&temporary, &destination).map_err(|_| ()))
        .is_ok();
    app.exit(if database_created && cleanup_complete && written {
        0
    } else {
        1
    });
}

#[tauri::command]
pub fn desktop_backend_info(state: State<'_, BackendState>) -> Result<DesktopBackendInfo, String> {
    state.info()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::OsStr;
    use std::sync::{Arc, Mutex as TestMutex};
    use std::time::{SystemTime, UNIX_EPOCH};

    struct FakeOwnedProcess {
        events: Arc<TestMutex<Vec<&'static str>>>,
        exited: bool,
        wait_result: Result<bool, String>,
        finalize_result: Result<(), String>,
        force_result: Result<(), String>,
    }

    impl OwnedProcessControl for FakeOwnedProcess {
        fn sidecar_exited(&mut self) -> Result<bool, String> {
            self.events.lock().unwrap().push("status");
            Ok(self.exited)
        }

        fn wait_for_sidecar_exit(&mut self, _timeout: Duration) -> Result<bool, String> {
            self.events.lock().unwrap().push("wait");
            self.wait_result.clone()
        }

        fn finalize_after_graceful(&mut self) -> Result<(), String> {
            self.events.lock().unwrap().push("finalize");
            self.finalize_result.clone()
        }

        fn force_cleanup(&mut self) -> Result<(), String> {
            self.events.lock().unwrap().push("force");
            self.force_result.clone()
        }
    }

    fn fake_owned(events: &Arc<TestMutex<Vec<&'static str>>>) -> FakeOwnedProcess {
        FakeOwnedProcess {
            events: Arc::clone(events),
            exited: false,
            wait_result: Ok(true),
            finalize_result: Ok(()),
            force_result: Ok(()),
        }
    }

    struct FakeBackend {
        stops: Arc<TestMutex<usize>>,
        reported_ready_pid: u32,
        unrelated_process_terminated: Arc<TestMutex<bool>>,
    }

    impl StoppableBackend for FakeBackend {
        fn stop_backend(&mut self) -> Result<(), String> {
            let _diagnostic_only = self.reported_ready_pid;
            *self.stops.lock().unwrap() += 1;
            assert!(!*self.unrelated_process_terminated.lock().unwrap());
            Ok(())
        }
    }

    fn smoke_test_root(label: &str) -> std::path::PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock after epoch")
            .as_nanos();
        std::env::temp_dir().join(format!(
            "qian-rc-smoke-{label}-{}-{nonce}",
            std::process::id()
        ))
    }

    #[test]
    fn parses_only_loopback_ready_payloads() {
        let ready = parse_ready_payload(br#"{"host":"127.0.0.1","port":43123,"pid":77}"#)
            .expect("valid loopback READY payload");
        assert_eq!(
            ready,
            SidecarReady {
                host: "127.0.0.1".into(),
                port: 43123,
                pid: 77,
            }
        );

        assert!(parse_ready_payload(br#"{"host":"0.0.0.0","port":43123,"pid":77}"#).is_err());
        assert!(parse_ready_payload(br#"{"host":"127.0.0.1","port":0,"pid":77}"#).is_err());
        assert!(parse_ready_payload(br#"{"host":"127.0.0.1","port":43123,"pid":0}"#).is_err());
    }

    #[test]
    fn generated_launch_tokens_are_32_random_bytes_encoded_as_hex() {
        let first = random_launch_token();
        let second = random_launch_token();
        assert_eq!(first.len(), 64);
        assert_eq!(second.len(), 64);
        assert!(first.chars().all(|value| value.is_ascii_hexdigit()));
        assert_ne!(first, second);
    }

    #[test]
    fn cold_packaged_sidecar_gets_a_realistic_ready_budget() {
        assert!(SIDECAR_READY_TIMEOUT >= Duration::from_secs(45));
    }

    #[test]
    fn stop_attempts_owned_cleanup_after_graceful_shutdown_failure() {
        let events = Arc::new(TestMutex::new(Vec::new()));
        let mut owned = fake_owned(&events);

        let result = shutdown_owned_process(&mut owned, || {
            events.lock().unwrap().push("graceful");
            Err("DESKTOP_GRACEFUL_SHUTDOWN_FAILED".to_string())
        });

        assert_eq!(result, Ok(()));
        assert_eq!(
            *events.lock().unwrap(),
            vec!["status", "graceful", "wait", "force"]
        );
    }

    #[test]
    fn stop_attempts_all_cleanup_steps_when_one_step_fails() {
        let events = Arc::new(TestMutex::new(Vec::new()));
        let mut owned = fake_owned(&events);
        owned.wait_result = Err("DESKTOP_PROCESS_WAIT_FAILED".to_string());

        let result = shutdown_owned_process(&mut owned, || {
            events.lock().unwrap().push("graceful");
            Err("DESKTOP_GRACEFUL_SHUTDOWN_FAILED".to_string())
        });

        assert_eq!(result, Ok(()));
        assert_eq!(
            *events.lock().unwrap(),
            vec!["status", "graceful", "wait", "force"]
        );
    }

    #[test]
    fn clean_graceful_exit_skips_forced_cleanup() {
        let events = Arc::new(TestMutex::new(Vec::new()));
        let mut owned = fake_owned(&events);

        let result = shutdown_owned_process(&mut owned, || {
            events.lock().unwrap().push("graceful");
            Ok(())
        });

        assert_eq!(result, Ok(()));
        assert_eq!(
            *events.lock().unwrap(),
            vec!["status", "graceful", "wait", "finalize"]
        );
    }

    #[test]
    fn ready_pid_is_diagnostic_only_and_reused_pid_is_not_targeted() {
        let stops = Arc::new(TestMutex::new(0));
        let unrelated_process_terminated = Arc::new(TestMutex::new(false));
        let state = BackendState::<FakeBackend>::default();
        state
            .install(FakeBackend {
                stops: Arc::clone(&stops),
                reported_ready_pid: 4242,
                unrelated_process_terminated: Arc::clone(&unrelated_process_terminated),
            })
            .expect("install fake backend");

        assert_eq!(state.stop(), Ok(()));
        assert_eq!(state.stop(), Ok(()));
        assert_eq!(*stops.lock().unwrap(), 1);
        assert!(!*unrelated_process_terminated.lock().unwrap());
    }

    #[test]
    fn startup_failure_cleans_owned_process_tree() {
        let events = Arc::new(TestMutex::new(Vec::new()));
        let mut owned = fake_owned(&events);

        let error = owned_startup_failure(&mut owned, "DESKTOP_READY_JSON_INVALID");

        assert_eq!(error.to_string(), "DESKTOP_READY_JSON_INVALID");
        assert_eq!(*events.lock().unwrap(), vec!["force"]);
    }

    #[test]
    fn bundled_sidecar_is_resolved_next_to_the_desktop_executable() {
        let executable = Path::new("candidate").join(if cfg!(windows) {
            "qian-labor-desktop.exe"
        } else {
            "qian-labor-desktop"
        });
        let expected = executable
            .parent()
            .expect("candidate directory")
            .join(if cfg!(windows) {
                "qian-sidecar.exe"
            } else {
                "qian-sidecar"
            });

        assert_eq!(
            bundled_sidecar_path(&executable).expect("sidecar path"),
            expected
        );
    }

    #[test]
    fn ready_file_is_randomized_inside_the_sidecar_data_directory() {
        let data_dir = Path::new("candidate-data");
        let first = ready_file_path(data_dir);
        let second = ready_file_path(data_dir);

        assert_eq!(first.parent(), Some(data_dir));
        assert_eq!(second.parent(), Some(data_dir));
        assert!(first
            .file_name()
            .and_then(OsStr::to_str)
            .is_some_and(
                |name| name.starts_with(".qian-sidecar-ready-") && name.ends_with(".json")
            ));
        assert_ne!(first, second);
    }

    #[test]
    fn windows_runtime_smoke_root_avoids_verbatim_canonical_paths() {
        let requested = Path::new(r"D:\runner-temp\qian-rc-smoke-123");
        let canonical = Path::new(r"\\?\D:\runner-temp\qian-rc-smoke-123");

        assert_eq!(runtime_smoke_root(requested, canonical, true), requested);
        assert_eq!(runtime_smoke_root(requested, canonical, false), canonical);
    }

    #[test]
    fn packaged_smoke_failure_codes_are_strictly_bounded() {
        assert!(stable_failure_code("DESKTOP_SIDECAR_SPAWN_FAILED"));
        assert!(!stable_failure_code("private path"));
        assert!(!stable_failure_code("DESKTOP_FAILURE\nINJECTED"));
        assert!(!stable_failure_code(&"A".repeat(161)));
    }

    #[test]
    fn smoke_root_requires_explicit_flag_existing_temp_child_and_prefix() {
        let root = smoke_test_root("valid");
        std::fs::create_dir(&root).expect("create smoke root");

        assert!(validated_smoke_root(Some(OsStr::new("0")), Some(root.as_os_str())).is_err());
        assert!(validated_smoke_root(Some(OsStr::new("1")), None).is_err());
        assert!(validated_smoke_root(Some(OsStr::new("1")), Some(root.as_os_str())).is_ok());

        let missing = smoke_test_root("missing");
        assert!(validated_smoke_root(Some(OsStr::new("1")), Some(missing.as_os_str())).is_err());

        let wrong_prefix = std::env::temp_dir().join(format!("wrong-prefix-{nonce}", nonce = 1));
        std::fs::create_dir(&wrong_prefix).expect("create wrong-prefix root");
        assert!(
            validated_smoke_root(Some(OsStr::new("1")), Some(wrong_prefix.as_os_str())).is_err()
        );

        std::fs::remove_dir_all(root).expect("remove smoke root");
        std::fs::remove_dir_all(wrong_prefix).expect("remove wrong-prefix root");
    }

    #[cfg(unix)]
    #[test]
    fn smoke_root_rejects_a_symlink_that_resolves_outside_temp() {
        use std::os::unix::fs::symlink;

        let link = smoke_test_root("link");
        symlink("/", &link).expect("create smoke symlink");

        assert!(validated_smoke_root(Some(OsStr::new("1")), Some(link.as_os_str())).is_err());

        std::fs::remove_file(link).expect("remove smoke symlink");
    }
}
