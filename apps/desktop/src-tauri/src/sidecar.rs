use std::ffi::OsStr;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use anyhow::{anyhow, Context, Result};
use rand::RngCore;
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager, State};

const READY_FILE_PREFIX: &str = ".qian-sidecar-ready-";
const LOOPBACK_HOST: &str = "127.0.0.1";
const SMOKE_FLAG: &str = "QIAN_RC_SMOKE";
const SMOKE_ROOT: &str = "QIAN_RC_SMOKE_DIR";
const SMOKE_PREFIX: &str = "qian-rc-smoke-";

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
    child: Child,
    data_dir: PathBuf,
    sidecar_pid: u32,
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

#[derive(Default)]
pub struct BackendState {
    process: Mutex<Option<BackendProcess>>,
}

impl BackendState {
    pub fn install(&self, process: BackendProcess) -> Result<(), String> {
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

    fn info(&self) -> Result<DesktopBackendInfo, String> {
        let slot = self
            .process
            .lock()
            .map_err(|_| "DESKTOP_BACKEND_STATE_POISONED".to_string())?;
        slot.as_ref()
            .map(|process| process.info.clone())
            .ok_or_else(|| "DESKTOP_BACKEND_NOT_READY".to_string())
    }

    pub fn stop(&self) -> Result<(), String> {
        let process = self
            .process
            .lock()
            .map_err(|_| "DESKTOP_BACKEND_STATE_POISONED".to_string())?
            .take();
        if let Some(mut process) = process {
            terminate_ready_process(process.sidecar_pid)?;
            stop_child(&mut process.child)?;
        }
        Ok(())
    }
}

fn stop_child(child: &mut Child) -> Result<(), String> {
    if child
        .try_wait()
        .map_err(|_| "DESKTOP_BACKEND_STOP_FAILED".to_string())?
        .is_none()
    {
        child
            .kill()
            .map_err(|_| "DESKTOP_BACKEND_STOP_FAILED".to_string())?;
        child
            .wait()
            .map_err(|_| "DESKTOP_BACKEND_STOP_FAILED".to_string())?;
    }
    Ok(())
}

#[cfg(unix)]
fn terminate_ready_process(pid: u32) -> Result<(), String> {
    let result = unsafe { libc::kill(pid as libc::pid_t, libc::SIGTERM) };
    if result == 0 {
        return Ok(());
    }
    let error = std::io::Error::last_os_error();
    if error.raw_os_error() == Some(libc::ESRCH) {
        Ok(())
    } else {
        Err("DESKTOP_BACKEND_STOP_FAILED".to_string())
    }
}

#[cfg(windows)]
fn terminate_ready_process(pid: u32) -> Result<(), String> {
    use windows_sys::Win32::Foundation::CloseHandle;
    use windows_sys::Win32::System::Threading::{OpenProcess, TerminateProcess, PROCESS_TERMINATE};

    let handle = unsafe { OpenProcess(PROCESS_TERMINATE, 0, pid) };
    if handle.is_null() {
        return Ok(());
    }
    let terminated = unsafe { TerminateProcess(handle, 0) };
    unsafe {
        CloseHandle(handle);
    }
    if terminated == 0 {
        Err("DESKTOP_BACKEND_STOP_FAILED".to_string())
    } else {
        Ok(())
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

#[cfg(windows)]
fn configure_sidecar_process(command: &mut Command) {
    use std::os::windows::process::CommandExt;

    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    command.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(not(windows))]
fn configure_sidecar_process(_command: &mut Command) {}

fn validated_smoke_root(flag: Option<&OsStr>, root: Option<&OsStr>) -> Result<PathBuf, String> {
    if flag != Some(OsStr::new("1")) {
        return Err("RC_SMOKE_FLAG_INVALID".to_string());
    }
    let requested = root.ok_or_else(|| "RC_SMOKE_ROOT_REQUIRED".to_string())?;
    let requested_path = Path::new(requested);
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
    Ok(canonical_root)
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
    let mut command = Command::new(&sidecar);
    command
        .current_dir(sidecar_directory)
        .env("QIAN_DESKTOP_DATA_DIR", data_dir.as_os_str())
        .env("QIAN_DESKTOP_TOKEN", &token)
        .env("QIAN_DESKTOP_PORT", "0")
        .env("QIAN_DESKTOP_READY_FILE", ready_file.as_os_str())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    configure_sidecar_process(&mut command);

    let mut child = command.spawn().context("DESKTOP_SIDECAR_SPAWN_FAILED")?;

    let deadline = tokio::time::Instant::now() + Duration::from_secs(15);
    let ready = loop {
        match read_ready_file(&ready_file) {
            Ok(Some(payload)) => match parse_ready_payload(&payload) {
                Ok(ready) => {
                    cleanup_ready_files(&ready_file);
                    let exited = match child.try_wait() {
                        Ok(status) => status.is_some(),
                        Err(_) => {
                            let _ = stop_child(&mut child);
                            return Err(anyhow!("DESKTOP_SIDECAR_EVENT_ERROR"));
                        }
                    };
                    if exited {
                        return Err(anyhow!("DESKTOP_SIDECAR_TERMINATED_BEFORE_READY"));
                    }
                    break ready;
                }
                Err(error) => {
                    cleanup_ready_files(&ready_file);
                    let _ = stop_child(&mut child);
                    return Err(anyhow::Error::msg(error));
                }
            },
            Ok(None) => {}
            Err(error) => {
                cleanup_ready_files(&ready_file);
                let _ = stop_child(&mut child);
                return Err(anyhow::Error::msg(error));
            }
        }

        let exited = match child.try_wait() {
            Ok(status) => status.is_some(),
            Err(_) => {
                let _ = stop_child(&mut child);
                return Err(anyhow!("DESKTOP_SIDECAR_EVENT_ERROR"));
            }
        };
        if exited {
            cleanup_ready_files(&ready_file);
            return Err(anyhow!("DESKTOP_SIDECAR_TERMINATED_BEFORE_READY"));
        }
        if tokio::time::Instant::now() >= deadline {
            cleanup_ready_files(&ready_file);
            let _ = stop_child(&mut child);
            return Err(anyhow!("DESKTOP_SIDECAR_READY_TIMEOUT"));
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    };

    Ok(BackendProcess {
        info: DesktopBackendInfo {
            base_url: format!("http://{}:{}", ready.host, ready.port),
            token,
        },
        child,
        data_dir,
        sidecar_pid: ready.pid,
        smoke_root,
    })
}

pub async fn run_packaged_smoke(app: AppHandle, context: PackagedSmokeContext) {
    let database = context.data_dir.join("qian-labor.db");
    let deadline = tokio::time::Instant::now() + Duration::from_secs(15);
    while !database.is_file() && tokio::time::Instant::now() < deadline {
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let database_created = database.is_file();
    let result = serde_json::json!({
        "database_created": database_created,
        "sidecar_pid": context.sidecar_pid,
    });
    let temporary = context.root.join("result.json.tmp");
    let destination = context.root.join("result.json");
    let written = serde_json::to_vec(&result)
        .map_err(|_| ())
        .and_then(|bytes| std::fs::write(&temporary, bytes).map_err(|_| ()))
        .and_then(|_| std::fs::rename(&temporary, &destination).map_err(|_| ()))
        .is_ok();
    app.exit(if database_created && written { 0 } else { 1 });
}

#[tauri::command]
pub fn desktop_backend_info(state: State<'_, BackendState>) -> Result<DesktopBackendInfo, String> {
    state.info()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::OsStr;
    use std::time::{SystemTime, UNIX_EPOCH};

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
