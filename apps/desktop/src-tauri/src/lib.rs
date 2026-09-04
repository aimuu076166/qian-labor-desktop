mod credentials;
mod process_ownership;
mod sidecar;

use std::io;

use credentials::{
    configure_zhipu_provider, mark_zhipu_provider_validated, provider_configuration_status,
};
use sidecar::{
    desktop_backend_info, record_packaged_smoke_failure, run_packaged_smoke, start_backend,
    BackendProcess, BackendState,
};
use tauri::{Manager, RunEvent};

fn startup_error(code: &'static str) -> Box<dyn std::error::Error> {
    Box::new(io::Error::other(code))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(BackendState::<BackendProcess>::default())
        .invoke_handler(tauri::generate_handler![
            desktop_backend_info,
            provider_configuration_status,
            configure_zhipu_provider,
            mark_zhipu_provider_validated
        ])
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
