#[cfg(windows)]
use std::ffi::OsStr;
use std::ffi::OsString;
use std::path::Path;
use std::time::{Duration, Instant};

const PROCESS_ERROR: &str = "DESKTOP_OWNED_PROCESS_ERROR";
const PROCESS_CLEANUP_ERROR: &str = "DESKTOP_OWNED_PROCESS_CLEANUP_FAILED";

struct SpawnSpec<'a> {
    executable: &'a Path,
    current_dir: &'a Path,
    args: Vec<OsString>,
    env: Vec<(OsString, OsString)>,
}

impl<'a> SpawnSpec<'a> {
    fn sidecar(
        executable: &'a Path,
        current_dir: &'a Path,
        data_dir: &'a Path,
        token: &'a str,
        ready_file: &'a Path,
    ) -> Self {
        Self {
            executable,
            current_dir,
            args: Vec::new(),
            env: vec![
                (
                    OsString::from("QIAN_DESKTOP_DATA_DIR"),
                    data_dir.as_os_str().to_owned(),
                ),
                (OsString::from("QIAN_DESKTOP_TOKEN"), OsString::from(token)),
                (OsString::from("QIAN_DESKTOP_PORT"), OsString::from("0")),
                (
                    OsString::from("QIAN_DESKTOP_READY_FILE"),
                    ready_file.as_os_str().to_owned(),
                ),
            ],
        }
    }
}

pub struct OwnedSidecarProcess {
    platform: PlatformOwnedProcess,
}

impl OwnedSidecarProcess {
    pub fn spawn_sidecar(
        executable: &Path,
        current_dir: &Path,
        data_dir: &Path,
        token: &str,
        ready_file: &Path,
    ) -> Result<Self, String> {
        let spec = SpawnSpec::sidecar(executable, current_dir, data_dir, token, ready_file);
        PlatformOwnedProcess::spawn(&spec).map(|platform| Self { platform })
    }

    pub fn sidecar_exited(&mut self) -> Result<bool, String> {
        self.platform.sidecar_exited()
    }

    pub fn wait_for_sidecar_exit(&mut self, timeout: Duration) -> Result<bool, String> {
        self.platform.wait_for_sidecar_exit(timeout)
    }

    pub fn finalize_after_graceful(&mut self) -> Result<(), String> {
        self.platform.finalize_after_graceful()
    }

    pub fn force_cleanup(&mut self) -> Result<(), String> {
        self.platform.force_cleanup()
    }
}

impl Drop for OwnedSidecarProcess {
    fn drop(&mut self) {
        let _ = self.platform.force_cleanup();
    }
}

#[cfg(unix)]
struct PlatformOwnedProcess {
    anchor: std::process::Child,
    sidecar: std::process::Child,
    watchdog: std::os::unix::net::UnixStream,
    process_group: libc::pid_t,
    sidecar_has_exited: bool,
    cleaned: bool,
}

#[cfg(unix)]
impl PlatformOwnedProcess {
    fn spawn(spec: &SpawnSpec<'_>) -> Result<Self, String> {
        use std::os::fd::OwnedFd;
        use std::os::unix::net::UnixStream;
        use std::os::unix::process::CommandExt;
        use std::process::{Command, Stdio};

        let (anchor_input, watchdog) = UnixStream::pair().map_err(|_| PROCESS_ERROR.to_string())?;
        let anchor_input: OwnedFd = anchor_input.into();
        let mut anchor = Command::new("/bin/sh");
        anchor
            .arg("-c")
            .arg(
                "trap '' TERM; IFS= read -r _ || true; kill -TERM -- -$$; /bin/sleep 1; kill -KILL -- -$$",
            )
            .current_dir(spec.current_dir)
            .stdin(Stdio::from(anchor_input))
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .process_group(0);
        let mut anchor = anchor.spawn().map_err(|_| PROCESS_ERROR.to_string())?;
        let process_group = anchor.id() as libc::pid_t;

        let mut command = Command::new(spec.executable);
        command
            .args(&spec.args)
            .current_dir(spec.current_dir)
            .envs(spec.env.iter().map(|(key, value)| (key, value)))
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .process_group(process_group);

        let sidecar = match command.spawn() {
            Ok(child) => child,
            Err(_) => {
                terminate_verified_group(&mut anchor, process_group);
                return Err(PROCESS_ERROR.to_string());
            }
        };

        Ok(Self {
            anchor,
            sidecar,
            watchdog,
            process_group,
            sidecar_has_exited: false,
            cleaned: false,
        })
    }

    fn sidecar_exited(&mut self) -> Result<bool, String> {
        if self.sidecar_has_exited {
            return Ok(true);
        }
        match self.sidecar.try_wait() {
            Ok(Some(_)) => {
                self.sidecar_has_exited = true;
                Ok(true)
            }
            Ok(None) => Ok(false),
            Err(_) => Err(PROCESS_ERROR.to_string()),
        }
    }

    fn wait_for_sidecar_exit(&mut self, timeout: Duration) -> Result<bool, String> {
        let deadline = Instant::now() + timeout;
        loop {
            if self.sidecar_exited()? {
                return Ok(true);
            }
            if Instant::now() >= deadline {
                return Ok(false);
            }
            std::thread::sleep(Duration::from_millis(25));
        }
    }

    fn verify_anchor_ownership(&mut self) -> Result<(), String> {
        if self.cleaned {
            return Ok(());
        }
        if self
            .anchor
            .try_wait()
            .map_err(|_| PROCESS_ERROR.to_string())?
            .is_some()
        {
            return Err("DESKTOP_PROCESS_OWNERSHIP_UNPROVABLE".to_string());
        }
        let actual_group = unsafe { libc::getpgid(self.anchor.id() as libc::pid_t) };
        if actual_group != self.process_group {
            return Err("DESKTOP_PROCESS_OWNERSHIP_UNPROVABLE".to_string());
        }
        Ok(())
    }

    fn signal_verified_group(&mut self, signal: libc::c_int) -> Result<(), String> {
        self.verify_anchor_ownership()?;
        let result = unsafe { libc::kill(-self.process_group, signal) };
        if result == 0 {
            Ok(())
        } else {
            Err(PROCESS_CLEANUP_ERROR.to_string())
        }
    }

    fn cleanup_group(&mut self) -> Result<(), String> {
        if self.cleaned {
            return Ok(());
        }

        self.signal_verified_group(libc::SIGTERM)?;
        let _ = self.wait_for_sidecar_exit(Duration::from_millis(500));
        self.signal_verified_group(libc::SIGKILL)?;

        self.anchor
            .wait()
            .map_err(|_| PROCESS_CLEANUP_ERROR.to_string())?;
        self.sidecar
            .wait()
            .map_err(|_| PROCESS_CLEANUP_ERROR.to_string())?;
        self.sidecar_has_exited = true;
        self.cleaned = true;
        let _ = self.watchdog.shutdown(std::net::Shutdown::Both);
        Ok(())
    }

    fn finalize_after_graceful(&mut self) -> Result<(), String> {
        if !self.sidecar_exited()? {
            return Err(PROCESS_CLEANUP_ERROR.to_string());
        }
        self.cleanup_group()
    }

    fn force_cleanup(&mut self) -> Result<(), String> {
        self.cleanup_group()
    }
}

#[cfg(unix)]
fn terminate_verified_group(anchor: &mut std::process::Child, process_group: libc::pid_t) {
    let anchor_is_owned = anchor.try_wait().ok().flatten().is_none()
        && unsafe { libc::getpgid(anchor.id() as libc::pid_t) } == process_group;
    if anchor_is_owned {
        unsafe {
            libc::kill(-process_group, libc::SIGKILL);
        }
    }
    let _ = anchor.wait();
}

#[cfg(windows)]
struct PlatformOwnedProcess {
    process: windows_sys::Win32::Foundation::HANDLE,
    job: windows_sys::Win32::Foundation::HANDLE,
    cleaned: bool,
}

#[cfg(windows)]
impl PlatformOwnedProcess {
    fn spawn(spec: &SpawnSpec<'_>) -> Result<Self, String> {
        use std::ptr::null;
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::JobObjects::{
            AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
            SetInformationJobObject, TerminateJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        };
        use windows_sys::Win32::System::Threading::{
            CreateProcessW, ResumeThread, TerminateProcess, CREATE_NO_WINDOW, CREATE_SUSPENDED,
            CREATE_UNICODE_ENVIRONMENT, PROCESS_INFORMATION, STARTUPINFOW,
        };

        let job = unsafe { CreateJobObjectW(null(), null()) };
        if job.is_null() {
            return Err(PROCESS_ERROR.to_string());
        }
        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let configured = unsafe {
            SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        };
        if configured == 0 {
            unsafe { CloseHandle(job) };
            return Err(PROCESS_ERROR.to_string());
        }

        let executable = wide_null(spec.executable.as_os_str());
        let current_dir = wide_null(spec.current_dir.as_os_str());
        let mut command_line = windows_command_line(spec.executable.as_os_str(), &spec.args);
        let environment = windows_environment_block(&spec.env);
        let mut startup = STARTUPINFOW::default();
        startup.cb = std::mem::size_of::<STARTUPINFOW>() as u32;
        let mut information = PROCESS_INFORMATION::default();
        let created = unsafe {
            CreateProcessW(
                executable.as_ptr(),
                command_line.as_mut_ptr(),
                null(),
                null(),
                0,
                CREATE_NO_WINDOW | CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT,
                environment.as_ptr().cast(),
                current_dir.as_ptr(),
                &startup,
                &mut information,
            )
        };
        if created == 0 {
            unsafe { CloseHandle(job) };
            return Err(PROCESS_ERROR.to_string());
        }

        if unsafe { AssignProcessToJobObject(job, information.hProcess) } == 0 {
            unsafe {
                TerminateProcess(information.hProcess, 1);
                CloseHandle(information.hThread);
                CloseHandle(information.hProcess);
                CloseHandle(job);
            }
            return Err(PROCESS_ERROR.to_string());
        }

        if unsafe { ResumeThread(information.hThread) } == u32::MAX {
            unsafe {
                TerminateJobObject(job, 1);
                CloseHandle(information.hThread);
                CloseHandle(information.hProcess);
                CloseHandle(job);
            }
            return Err(PROCESS_ERROR.to_string());
        }
        unsafe { CloseHandle(information.hThread) };

        Ok(Self {
            process: information.hProcess,
            job,
            cleaned: false,
        })
    }

    fn active_processes(&self) -> Result<u32, String> {
        use windows_sys::Win32::System::JobObjects::{
            JobObjectBasicAccountingInformation, QueryInformationJobObject,
            JOBOBJECT_BASIC_ACCOUNTING_INFORMATION,
        };

        let mut accounting = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION::default();
        let queried = unsafe {
            QueryInformationJobObject(
                self.job,
                JobObjectBasicAccountingInformation,
                (&mut accounting as *mut JOBOBJECT_BASIC_ACCOUNTING_INFORMATION).cast(),
                std::mem::size_of::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>() as u32,
                std::ptr::null_mut(),
            )
        };
        if queried == 0 {
            Err(PROCESS_ERROR.to_string())
        } else {
            Ok(accounting.ActiveProcesses)
        }
    }

    fn sidecar_exited(&mut self) -> Result<bool, String> {
        if self.cleaned {
            return Ok(true);
        }
        self.active_processes().map(|count| count == 0)
    }

    fn wait_for_sidecar_exit(&mut self, timeout: Duration) -> Result<bool, String> {
        let deadline = Instant::now() + timeout;
        loop {
            if self.sidecar_exited()? {
                return Ok(true);
            }
            if Instant::now() >= deadline {
                return Ok(false);
            }
            std::thread::sleep(Duration::from_millis(25));
        }
    }

    fn close_handles(&mut self) {
        if self.cleaned {
            return;
        }
        unsafe {
            windows_sys::Win32::Foundation::CloseHandle(self.process);
            windows_sys::Win32::Foundation::CloseHandle(self.job);
        }
        self.cleaned = true;
    }

    fn finalize_after_graceful(&mut self) -> Result<(), String> {
        if !self.sidecar_exited()? {
            return Err(PROCESS_CLEANUP_ERROR.to_string());
        }
        self.close_handles();
        Ok(())
    }

    fn force_cleanup(&mut self) -> Result<(), String> {
        if self.cleaned {
            return Ok(());
        }
        use windows_sys::Win32::System::JobObjects::TerminateJobObject;
        if unsafe { TerminateJobObject(self.job, 1) } == 0 {
            return Err(PROCESS_CLEANUP_ERROR.to_string());
        }
        if !self.wait_for_sidecar_exit(Duration::from_secs(5))? {
            return Err(PROCESS_CLEANUP_ERROR.to_string());
        }
        self.close_handles();
        Ok(())
    }
}

#[cfg(windows)]
fn wide_null(value: &OsStr) -> Vec<u16> {
    use std::os::windows::ffi::OsStrExt;
    value.encode_wide().chain(std::iter::once(0)).collect()
}

#[cfg(windows)]
fn quote_windows_argument(value: &OsStr) -> OsString {
    let text = value.to_string_lossy();
    if !text.is_empty() && !text.chars().any(|ch| ch.is_whitespace() || ch == '"') {
        return value.to_owned();
    }
    let mut quoted = String::from("\"");
    let mut backslashes = 0;
    for character in text.chars() {
        if character == '\\' {
            backslashes += 1;
        } else if character == '"' {
            quoted.push_str(&"\\".repeat(backslashes * 2 + 1));
            quoted.push('"');
            backslashes = 0;
        } else {
            quoted.push_str(&"\\".repeat(backslashes));
            backslashes = 0;
            quoted.push(character);
        }
    }
    quoted.push_str(&"\\".repeat(backslashes * 2));
    quoted.push('"');
    OsString::from(quoted)
}

#[cfg(windows)]
fn windows_command_line(executable: &OsStr, args: &[OsString]) -> Vec<u16> {
    use std::os::windows::ffi::OsStrExt;
    let mut values = Vec::with_capacity(args.len() + 1);
    values.push(quote_windows_argument(executable));
    values.extend(args.iter().map(|value| quote_windows_argument(value)));
    values
        .iter()
        .enumerate()
        .flat_map(|(index, value)| {
            let separator = (index > 0).then_some(' ' as u16);
            separator
                .into_iter()
                .chain(value.encode_wide())
                .collect::<Vec<_>>()
        })
        .chain(std::iter::once(0))
        .collect()
}

#[cfg(windows)]
fn windows_environment_block(overrides: &[(OsString, OsString)]) -> Vec<u16> {
    use std::os::windows::ffi::OsStrExt;

    let mut values: Vec<(OsString, OsString)> = std::env::vars_os().collect();
    for (key, value) in overrides {
        let key_text = key.to_string_lossy();
        values
            .retain(|(candidate, _)| !candidate.to_string_lossy().eq_ignore_ascii_case(&key_text));
        values.push((key.clone(), value.clone()));
    }
    values.sort_by_key(|(key, _)| key.to_string_lossy().to_ascii_lowercase());

    let mut block = Vec::new();
    for (key, value) in values {
        block.extend(key.encode_wide());
        block.push('=' as u16);
        block.extend(value.encode_wide());
        block.push(0);
    }
    block.push(0);
    block
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::process::{Command, Stdio};
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temporary_directory(label: &str) -> std::path::PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock after epoch")
            .as_nanos();
        std::env::temp_dir().join(format!(
            "qian-owned-process-{label}-{}-{nonce}",
            std::process::id()
        ))
    }

    #[test]
    fn owned_group_cleanup_does_not_target_unrelated_process() {
        let temp = temporary_directory("cleanup");
        std::fs::create_dir_all(&temp).expect("create process ownership test directory");
        let child_pid_file = temp.join("child.pid");
        let script = format!(
            "sleep 60 & printf '%s' $! > '{}'; wait",
            child_pid_file.display()
        );
        let spec = SpawnSpec {
            executable: Path::new("/bin/sh"),
            current_dir: &temp,
            args: vec![OsString::from("-c"), OsString::from(script)],
            env: Vec::new(),
        };
        let mut owned = PlatformOwnedProcess::spawn(&spec).expect("spawn owned process tree");
        let mut unrelated = Command::new("/bin/sleep")
            .arg("60")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn unrelated process");

        let deadline = Instant::now() + Duration::from_secs(5);
        while !child_pid_file.is_file() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(20));
        }
        assert!(child_pid_file.is_file(), "owned descendant did not start");

        owned.force_cleanup().expect("clean owned process tree");
        assert!(
            unrelated
                .try_wait()
                .expect("query unrelated process")
                .is_none(),
            "unrelated process must remain alive"
        );

        unrelated.kill().expect("stop unrelated process");
        unrelated.wait().expect("reap unrelated process");
        std::fs::remove_dir_all(temp).expect("remove process ownership test directory");
    }

    #[test]
    fn watchdog_eof_cleans_owned_group_after_owner_death() {
        let temp = temporary_directory("watchdog");
        std::fs::create_dir_all(&temp).expect("create watchdog test directory");
        let spec = SpawnSpec {
            executable: Path::new("/bin/sh"),
            current_dir: &temp,
            args: vec![OsString::from("-c"), OsString::from("sleep 60 & wait")],
            env: Vec::new(),
        };
        let mut owned = PlatformOwnedProcess::spawn(&spec).expect("spawn watchdog process tree");
        let mut unrelated = Command::new("/bin/sleep")
            .arg("60")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn unrelated process");

        owned
            .watchdog
            .shutdown(std::net::Shutdown::Write)
            .expect("simulate owner descriptor closure");
        assert!(
            owned
                .wait_for_sidecar_exit(Duration::from_secs(5))
                .expect("wait for watchdog cleanup"),
            "watchdog must terminate the sidecar tree"
        );
        assert!(
            unrelated
                .try_wait()
                .expect("query unrelated process")
                .is_none(),
            "watchdog must not target an unrelated process"
        );
        owned.anchor.wait().expect("reap watchdog anchor");
        owned.cleaned = true;

        unrelated.kill().expect("stop unrelated process");
        unrelated.wait().expect("reap unrelated process");
        std::fs::remove_dir_all(temp).expect("remove watchdog test directory");
    }
}

#[cfg(all(test, windows))]
mod windows_tests {
    use super::*;
    use std::process::{Command, Stdio};

    #[test]
    fn job_cleanup_removes_descendants_but_not_unrelated_process() {
        let current_dir = std::env::current_dir().expect("current directory");
        let spec = SpawnSpec {
            executable: Path::new("cmd.exe"),
            current_dir: &current_dir,
            args: vec![
                OsString::from("/C"),
                OsString::from(
                    "start /B cmd.exe /C ping -n 60 127.0.0.1 ^>NUL & ping -n 60 127.0.0.1 >NUL",
                ),
            ],
            env: Vec::new(),
        };
        let mut owned = PlatformOwnedProcess::spawn(&spec).expect("spawn owned Windows tree");
        let mut unrelated = Command::new("cmd.exe")
            .args(["/C", "ping -n 60 127.0.0.1 >NUL"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn unrelated Windows process");

        let deadline = Instant::now() + Duration::from_secs(5);
        while owned.active_processes().expect("query job") < 2 && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(20));
        }
        assert!(owned.active_processes().expect("query populated job") >= 2);

        owned.force_cleanup().expect("terminate owned Windows job");
        assert!(
            unrelated
                .try_wait()
                .expect("query unrelated process")
                .is_none(),
            "unrelated Windows process must remain alive"
        );

        unrelated.kill().expect("stop unrelated Windows process");
        unrelated.wait().expect("reap unrelated Windows process");
    }
}
