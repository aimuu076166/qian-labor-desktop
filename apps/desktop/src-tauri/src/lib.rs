mod sidecar;

use std::io;

use sidecar::{
    desktop_backend_info, record_packaged_smoke_failure, run_packaged_smoke, start_backend,
    BackendState,
};
use tauri::{Manager, RunEvent};

fn startup_error(code: &'static str) -> Box<dyn std::error::Error> {
    Box::new(io::Error::other(code))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .manage(BackendState::default())
        .invoke_handler(tauri::generate_handler![desktop_backend_info])
        .setup(|app| {
            let backend = match tauri::async_runtime::block_on(start_backend(app.handle().clone()))
            {
                Ok(backend) => backend,
                Err(error) => {
                    record_packaged_smoke_failure(&error);
                    return Err(startup_error("DESKTOP_BACKEND_START_FAILED"));
                }
            };
            let smoke_context = backend.packaged_smoke_context();
            app.state::<BackendState>()
                .install(backend)
                .map_err(|_| startup_error("DESKTOP_BACKEND_STATE_FAILED"))?;
            if let Some(context) = smoke_context {
                tauri::async_runtime::spawn(run_packaged_smoke(app.handle().clone(), context));
            } else if let Some(window) = app.get_webview_window("main") {
                window.show()?;
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building 企安用工 Desktop");

    app.run(|app_handle, event| {
        if matches!(event, RunEvent::ExitRequested { .. } | RunEvent::Exit) {
            if let Some(state) = app_handle.try_state::<BackendState>() {
                let _ = state.stop();
            }
        }
    });
}
