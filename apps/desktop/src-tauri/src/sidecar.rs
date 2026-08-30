use std::sync::Mutex;
use std::time::Duration;

use anyhow::{anyhow, Context, Result};
use rand::RngCore;
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager, State};
use tauri_plugin_shell::{
    process::{CommandChild, CommandEvent},
    ShellExt,
};

const READY_PREFIX: &str = "QIAN_DESKTOP_READY=";
const LOOPBACK_HOST: &str = "127.0.0.1";

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
    child: CommandChild,
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
        if let Some(process) = process {
            process
                .child
                .kill()
                .map_err(|_| "DESKTOP_BACKEND_STOP_FAILED".to_string())?;
        }
        Ok(())
    }
}

pub fn parse_ready_line(line: &str) -> Result<SidecarReady, String> {
    let payload = line
        .strip_prefix(READY_PREFIX)
        .ok_or_else(|| "DESKTOP_READY_PREFIX_INVALID".to_string())?;
    let ready: SidecarReady =
        serde_json::from_str(payload).map_err(|_| "DESKTOP_READY_JSON_INVALID".to_string())?;
    if ready.host != LOOPBACK_HOST {
        return Err("DESKTOP_READY_HOST_INVALID".to_string());
    }
    if ready.port == 0 {
        return Err("DESKTOP_READY_PORT_INVALID".to_string());
    }
    Ok(ready)
}

fn random_launch_token() -> String {
    let mut bytes = [0_u8; 32];
    rand::rng().fill_bytes(&mut bytes);
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

pub async fn start_backend(app: AppHandle) -> Result<BackendProcess> {
    let data_dir = app
        .path()
        .app_data_dir()
        .context("DESKTOP_APP_DATA_DIR_UNAVAILABLE")?;
    std::fs::create_dir_all(&data_dir).context("DESKTOP_APP_DATA_DIR_CREATE_FAILED")?;

    let token = random_launch_token();
    let command = app
        .shell()
        .sidecar("qian-sidecar")
        .context("DESKTOP_SIDECAR_COMMAND_UNAVAILABLE")?
        .env("QIAN_DESKTOP_DATA_DIR", data_dir.as_os_str())
        .env("QIAN_DESKTOP_TOKEN", &token)
        .env("QIAN_DESKTOP_PORT", "0");

    let (mut receiver, child) = command.spawn().context("DESKTOP_SIDECAR_SPAWN_FAILED")?;
    let expected_pid = child.pid();
    let mut child = Some(child);

    let ready_result = tokio::time::timeout(Duration::from_secs(15), async {
        let mut stdout_buffer = String::new();
        while let Some(event) = receiver.recv().await {
            match event {
                CommandEvent::Stdout(bytes) => {
                    let text = String::from_utf8(bytes)
                        .map_err(|_| anyhow!("DESKTOP_SIDECAR_STDOUT_INVALID"))?;
                    stdout_buffer.push_str(&text);
                    stdout_buffer.push('\n');
                    while let Some(newline) = stdout_buffer.find('\n') {
                        let line = stdout_buffer[..newline].trim_end_matches('\r').to_string();
                        stdout_buffer.drain(..=newline);
                        if line.starts_with(READY_PREFIX) {
                            return parse_ready_line(&line).map_err(anyhow::Error::msg);
                        }
                    }
                }
                CommandEvent::Terminated(_) => {
                    return Err(anyhow!("DESKTOP_SIDECAR_TERMINATED_BEFORE_READY"));
                }
                CommandEvent::Error(_) => {
                    return Err(anyhow!("DESKTOP_SIDECAR_EVENT_ERROR"));
                }
                CommandEvent::Stderr(_) => {}
                _ => {}
            }
        }
        Err(anyhow!("DESKTOP_SIDECAR_EVENT_STREAM_CLOSED"))
    })
    .await;

    let ready = match ready_result {
        Ok(Ok(ready)) => ready,
        Ok(Err(error)) => {
            if let Some(process) = child.take() {
                let _ = process.kill();
            }
            return Err(error);
        }
        Err(_) => {
            if let Some(process) = child.take() {
                let _ = process.kill();
            }
            return Err(anyhow!("DESKTOP_SIDECAR_READY_TIMEOUT"));
        }
    };

    if ready.pid != expected_pid {
        if let Some(process) = child.take() {
            let _ = process.kill();
        }
        return Err(anyhow!("DESKTOP_SIDECAR_PID_MISMATCH"));
    }

    Ok(BackendProcess {
        info: DesktopBackendInfo {
            base_url: format!("http://{}:{}", ready.host, ready.port),
            token,
        },
        child: child.expect("child must exist after successful startup"),
    })
}

#[tauri::command]
pub fn desktop_backend_info(state: State<'_, BackendState>) -> Result<DesktopBackendInfo, String> {
    state.info()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_only_loopback_ready_lines() {
        let ready =
            parse_ready_line(r#"QIAN_DESKTOP_READY={"host":"127.0.0.1","port":43123,"pid":77}"#)
                .expect("valid loopback READY line");
        assert_eq!(
            ready,
            SidecarReady {
                host: "127.0.0.1".into(),
                port: 43123,
                pid: 77,
            }
        );

        assert!(
            parse_ready_line(r#"QIAN_DESKTOP_READY={"host":"0.0.0.0","port":43123,"pid":77}"#)
                .is_err()
        );
        assert!(
            parse_ready_line(r#"QIAN_DESKTOP_READY={"host":"127.0.0.1","port":0,"pid":77}"#)
                .is_err()
        );
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
}
