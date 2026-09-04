use std::ffi::OsString;
use std::path::Path;

use rand::RngCore;
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager};

use crate::sidecar::{start_backend, BackendState};

const KEYCHAIN_SERVICE: &str = "cn.qianlabor.desktop";
const API_KEY_ACCOUNT: &str = "zhipu-api-key";
const PII_PEPPER_ACCOUNT: &str = "pii-hash-pepper";
const CONFIG_FILE: &str = "provider-config.json";
const ZHIPU_BASE_URL: &str = "https://open.bigmodel.cn/api/paas/v4";
const ZHIPU_MODEL: &str = "glm-5.3-flash";

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ProviderConfigurationInput {
    pub api_key: String,
    pub text_model: String,
    pub vision_model: String,
    pub base_url: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct ProviderConfigurationStatus {
    pub provider: String,
    pub configured: bool,
    pub validated: bool,
    pub text_model: String,
    pub vision_model: String,
    pub base_url: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct ProviderConfigurationFile {
    provider: String,
    text_model: String,
    vision_model: String,
    base_url: String,
    validated: bool,
}

pub trait SecretStore {
    fn get(&self, account: &str) -> Result<Option<Vec<u8>>, String>;
    fn set(&self, account: &str, value: &[u8]) -> Result<(), String>;
}

pub struct SystemSecretStore;

#[cfg(target_os = "macos")]
impl SecretStore for SystemSecretStore {
    fn get(&self, account: &str) -> Result<Option<Vec<u8>>, String> {
        use security_framework::passwords::{generic_password, PasswordOptions};

        match generic_password(PasswordOptions::new_generic_password(
            KEYCHAIN_SERVICE,
            account,
        )) {
            Ok(value) => Ok(Some(value)),
            Err(error) if error.code() == -25300 => Ok(None),
            Err(_) => Err("DESKTOP_CREDENTIAL_READ_FAILED".to_string()),
        }
    }

    fn set(&self, account: &str, value: &[u8]) -> Result<(), String> {
        security_framework::passwords::set_generic_password(KEYCHAIN_SERVICE, account, value)
            .map_err(|_| "DESKTOP_CREDENTIAL_WRITE_FAILED".to_string())
    }
}

#[cfg(not(target_os = "macos"))]
impl SecretStore for SystemSecretStore {
    fn get(&self, _account: &str) -> Result<Option<Vec<u8>>, String> {
        Ok(None)
    }

    fn set(&self, _account: &str, _value: &[u8]) -> Result<(), String> {
        Err("DESKTOP_CREDENTIAL_STORE_UNSUPPORTED".to_string())
    }
}

pub fn configure_provider<S: SecretStore>(
    store: &S,
    data_dir: &Path,
    input: ProviderConfigurationInput,
) -> Result<ProviderConfigurationStatus, String> {
    let api_key = input.api_key.trim();
    let text_model = input.text_model.trim();
    let vision_model = input.vision_model.trim();
    let base_url = input.base_url.trim().trim_end_matches('/');
    if !(8..=4096).contains(&api_key.len())
        || text_model != ZHIPU_MODEL
        || vision_model != ZHIPU_MODEL
        || base_url != ZHIPU_BASE_URL
    {
        return Err("DESKTOP_PROVIDER_CONFIGURATION_INVALID".to_string());
    }

    store.set(API_KEY_ACCOUNT, api_key.as_bytes())?;
    let pepper = match store.get(PII_PEPPER_ACCOUNT)? {
        Some(value) if value.len() >= 32 => value,
        _ => {
            let value = random_secret();
            store.set(PII_PEPPER_ACCOUNT, value.as_bytes())?;
            value.into_bytes()
        }
    };
    if pepper.len() < 32 {
        return Err("DESKTOP_CREDENTIAL_WRITE_FAILED".to_string());
    }

    let config = ProviderConfigurationFile {
        provider: "zhipu".to_string(),
        text_model: text_model.to_string(),
        vision_model: vision_model.to_string(),
        base_url: base_url.to_string(),
        validated: false,
    };
    write_configuration(data_dir, &config)?;
    provider_status(store, data_dir)
}

pub fn provider_status<S: SecretStore>(
    store: &S,
    data_dir: &Path,
) -> Result<ProviderConfigurationStatus, String> {
    let Some(config) = read_configuration(data_dir)? else {
        return Ok(unconfigured_status());
    };
    let key_present = store
        .get(API_KEY_ACCOUNT)?
        .is_some_and(|value| !value.is_empty());
    let pepper_present = store
        .get(PII_PEPPER_ACCOUNT)?
        .is_some_and(|value| value.len() >= 32);
    let configured = config.provider == "zhipu" && key_present && pepper_present;
    let current_configuration = is_current_configuration(&config);
    Ok(ProviderConfigurationStatus {
        provider: "zhipu".to_string(),
        configured,
        validated: configured && current_configuration && config.validated,
        text_model: ZHIPU_MODEL.to_string(),
        vision_model: ZHIPU_MODEL.to_string(),
        base_url: ZHIPU_BASE_URL.to_string(),
    })
}

pub fn mark_provider_validated(data_dir: &Path) -> Result<(), String> {
    let mut config = read_configuration(data_dir)?
        .ok_or_else(|| "DESKTOP_PROVIDER_NOT_CONFIGURED".to_string())?;
    config.validated = true;
    write_configuration(data_dir, &config)
}

pub fn provider_environment<S: SecretStore>(
    store: &S,
    data_dir: &Path,
) -> Result<Vec<(OsString, OsString)>, String> {
    let Some(config) = read_configuration(data_dir)? else {
        return Ok(Vec::new());
    };
    let Some(key) = store.get(API_KEY_ACCOUNT)? else {
        return Ok(Vec::new());
    };
    let Some(pepper) = store.get(PII_PEPPER_ACCOUNT)? else {
        return Ok(Vec::new());
    };
    let key = String::from_utf8(key).map_err(|_| "DESKTOP_CREDENTIAL_READ_FAILED".to_string())?;
    let pepper =
        String::from_utf8(pepper).map_err(|_| "DESKTOP_CREDENTIAL_READ_FAILED".to_string())?;
    if key.is_empty() || pepper.len() < 32 || !is_current_configuration(&config) {
        return Ok(Vec::new());
    }
    Ok(vec![
        (OsString::from("AI_PROVIDER"), OsString::from("zhipu")),
        (OsString::from("AI_API_KEY"), OsString::from(key)),
        (
            OsString::from("AI_BASE_URL"),
            OsString::from(ZHIPU_BASE_URL),
        ),
        (OsString::from("AI_TEXT_MODEL"), OsString::from(ZHIPU_MODEL)),
        (
            OsString::from("AI_VISION_MODEL"),
            OsString::from(ZHIPU_MODEL),
        ),
        (OsString::from("PII_HASH_PEPPER"), OsString::from(pepper)),
    ])
}

fn is_current_configuration(config: &ProviderConfigurationFile) -> bool {
    config.provider == "zhipu"
        && config.text_model == ZHIPU_MODEL
        && config.vision_model == ZHIPU_MODEL
        && config.base_url.trim_end_matches('/') == ZHIPU_BASE_URL
}

fn random_secret() -> String {
    let mut bytes = [0_u8; 32];
    rand::rng().fill_bytes(&mut bytes);
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn configuration_path(data_dir: &Path) -> std::path::PathBuf {
    data_dir.join(CONFIG_FILE)
}

fn read_configuration(data_dir: &Path) -> Result<Option<ProviderConfigurationFile>, String> {
    let path = configuration_path(data_dir);
    let metadata = match std::fs::symlink_metadata(&path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err("DESKTOP_PROVIDER_CONFIG_READ_FAILED".to_string()),
    };
    if !metadata.is_file() || metadata.file_type().is_symlink() || metadata.len() > 16_384 {
        return Err("DESKTOP_PROVIDER_CONFIG_INVALID".to_string());
    }
    let bytes =
        std::fs::read(path).map_err(|_| "DESKTOP_PROVIDER_CONFIG_READ_FAILED".to_string())?;
    serde_json::from_slice(&bytes)
        .map(Some)
        .map_err(|_| "DESKTOP_PROVIDER_CONFIG_INVALID".to_string())
}

fn write_configuration(data_dir: &Path, config: &ProviderConfigurationFile) -> Result<(), String> {
    std::fs::create_dir_all(data_dir)
        .map_err(|_| "DESKTOP_PROVIDER_CONFIG_WRITE_FAILED".to_string())?;
    let destination = configuration_path(data_dir);
    if std::fs::symlink_metadata(&destination)
        .is_ok_and(|metadata| metadata.file_type().is_symlink())
    {
        return Err("DESKTOP_PROVIDER_CONFIG_INVALID".to_string());
    }
    let temporary = data_dir.join(format!("{CONFIG_FILE}.tmp"));
    if std::fs::symlink_metadata(&temporary).is_ok_and(|metadata| metadata.file_type().is_symlink())
    {
        return Err("DESKTOP_PROVIDER_CONFIG_INVALID".to_string());
    }
    let bytes = serde_json::to_vec(config)
        .map_err(|_| "DESKTOP_PROVIDER_CONFIG_WRITE_FAILED".to_string())?;
    std::fs::write(&temporary, bytes)
        .map_err(|_| "DESKTOP_PROVIDER_CONFIG_WRITE_FAILED".to_string())?;
    std::fs::rename(&temporary, &destination)
        .map_err(|_| "DESKTOP_PROVIDER_CONFIG_WRITE_FAILED".to_string())
}

fn unconfigured_status() -> ProviderConfigurationStatus {
    ProviderConfigurationStatus {
        provider: "zhipu".to_string(),
        configured: false,
        validated: false,
        text_model: ZHIPU_MODEL.to_string(),
        vision_model: ZHIPU_MODEL.to_string(),
        base_url: ZHIPU_BASE_URL.to_string(),
    }
}

fn app_data_dir(app: &AppHandle) -> Result<std::path::PathBuf, String> {
    app.path()
        .app_data_dir()
        .map_err(|_| "DESKTOP_APP_DATA_DIR_UNAVAILABLE".to_string())
}

#[tauri::command]
pub fn provider_configuration_status(
    app: AppHandle,
) -> Result<ProviderConfigurationStatus, String> {
    provider_status(&SystemSecretStore, &app_data_dir(&app)?)
}

#[tauri::command]
pub async fn configure_zhipu_provider(
    app: AppHandle,
    input: ProviderConfigurationInput,
) -> Result<ProviderConfigurationStatus, String> {
    let data_dir = app_data_dir(&app)?;
    let status = configure_provider(&SystemSecretStore, &data_dir, input)?;
    app.state::<BackendState>().stop()?;
    let backend = start_backend(app.clone())
        .await
        .map_err(|_| "DESKTOP_BACKEND_START_FAILED".to_string())?;
    app.state::<BackendState>().install(backend)?;
    Ok(status)
}

#[tauri::command]
pub fn mark_zhipu_provider_validated(
    app: AppHandle,
) -> Result<ProviderConfigurationStatus, String> {
    let data_dir = app_data_dir(&app)?;
    mark_provider_validated(&data_dir)?;
    provider_status(&SystemSecretStore, &data_dir)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use std::sync::Mutex;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[derive(Default)]
    struct MemorySecretStore {
        values: Mutex<HashMap<String, Vec<u8>>>,
    }

    impl SecretStore for MemorySecretStore {
        fn get(&self, account: &str) -> Result<Option<Vec<u8>>, String> {
            Ok(self.values.lock().unwrap().get(account).cloned())
        }

        fn set(&self, account: &str, value: &[u8]) -> Result<(), String> {
            self.values
                .lock()
                .unwrap()
                .insert(account.to_string(), value.to_vec());
            Ok(())
        }
    }

    fn temporary_directory(label: &str) -> std::path::PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock after epoch")
            .as_nanos();
        std::env::temp_dir().join(format!(
            "qian-provider-config-{label}-{}-{nonce}",
            std::process::id()
        ))
    }

    fn input() -> ProviderConfigurationInput {
        ProviderConfigurationInput {
            api_key: "synthetic-test-key-value".to_string(),
            text_model: "glm-5.3-flash".to_string(),
            vision_model: "glm-5.3-flash".to_string(),
            base_url: ZHIPU_BASE_URL.to_string(),
        }
    }

    #[cfg(unix)]
    #[test]
    fn local_secret_store_persists_owner_only_secret() {
        use std::os::unix::fs::PermissionsExt;

        let directory = temporary_directory("local-secret");
        std::fs::create_dir_all(&directory).expect("create temporary directory");
        let store = LocalSecretStore::new(&directory);

        store
            .set(API_KEY_ACCOUNT, b"synthetic-test-key-value")
            .expect("store local secret");

        assert_eq!(
            store.get(API_KEY_ACCOUNT).expect("read local secret"),
            Some(b"synthetic-test-key-value".to_vec())
        );
        let mode = std::fs::metadata(directory.join("zhipu-api-key.secret"))
            .expect("read local secret metadata")
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(mode, 0o600);
        std::fs::remove_dir_all(directory).expect("remove temporary directory");
    }

    #[cfg(unix)]
    #[test]
    fn local_secret_store_rejects_symlink() {
        use std::os::unix::fs::symlink;

        let directory = temporary_directory("local-secret-symlink");
        std::fs::create_dir_all(&directory).expect("create temporary directory");
        let target = directory.join("unrelated.txt");
        std::fs::write(&target, b"synthetic-unrelated-value").expect("write symlink target");
        symlink(&target, directory.join("zhipu-api-key.secret")).expect("create secret symlink");
        let store = LocalSecretStore::new(&directory);

        assert_eq!(
            store.get(API_KEY_ACCOUNT).unwrap_err(),
            "DESKTOP_CREDENTIAL_READ_FAILED"
        );
        std::fs::remove_dir_all(directory).expect("remove temporary directory");
    }

    #[test]
    fn configuration_keeps_secrets_out_of_the_json_file() {
        let directory = temporary_directory("secret-boundary");
        std::fs::create_dir_all(&directory).expect("create temporary directory");
        let store = MemorySecretStore::default();

        let status = configure_provider(&store, &directory, input()).expect("configure provider");

        assert!(status.configured);
        assert!(!status.validated);
        assert_eq!(
            store.get(API_KEY_ACCOUNT).unwrap().unwrap(),
            b"synthetic-test-key-value"
        );
        assert!(store.get(PII_PEPPER_ACCOUNT).unwrap().unwrap().len() >= 32);
        let config = std::fs::read_to_string(directory.join(CONFIG_FILE)).expect("read config");
        assert!(!config.contains("synthetic-test-key-value"));
        assert!(!config.contains("pepper"));
        std::fs::remove_dir_all(directory).expect("remove temporary directory");
    }

    #[test]
    fn configured_secrets_are_injected_only_into_the_sidecar_environment() {
        let directory = temporary_directory("environment");
        std::fs::create_dir_all(&directory).expect("create temporary directory");
        let store = MemorySecretStore::default();
        configure_provider(&store, &directory, input()).expect("configure provider");

        let environment = provider_environment(&store, &directory).expect("provider environment");
        let values: HashMap<_, _> = environment.into_iter().collect();

        assert_eq!(values.get(&OsString::from("AI_PROVIDER")).unwrap(), "zhipu");
        assert_eq!(
            values.get(&OsString::from("AI_API_KEY")).unwrap(),
            "synthetic-test-key-value"
        );
        assert_eq!(
            values.get(&OsString::from("AI_TEXT_MODEL")).unwrap(),
            "glm-5.3-flash"
        );
        assert_eq!(
            values.get(&OsString::from("AI_VISION_MODEL")).unwrap(),
            "glm-5.3-flash"
        );
        assert!(
            values
                .get(&OsString::from("PII_HASH_PEPPER"))
                .unwrap()
                .to_string_lossy()
                .len()
                >= 32
        );
        std::fs::remove_dir_all(directory).expect("remove temporary directory");
    }

    #[test]
    fn validation_state_is_persisted_without_persisting_secrets() {
        let directory = temporary_directory("validated");
        std::fs::create_dir_all(&directory).expect("create temporary directory");
        let store = MemorySecretStore::default();
        configure_provider(&store, &directory, input()).expect("configure provider");

        mark_provider_validated(&directory).expect("mark provider validated");

        let status = provider_status(&store, &directory).expect("provider status");
        assert!(status.configured);
        assert!(status.validated);
        std::fs::remove_dir_all(directory).expect("remove temporary directory");
    }

    #[test]
    fn empty_key_or_model_is_rejected_before_any_secret_is_written() {
        let directory = temporary_directory("invalid");
        std::fs::create_dir_all(&directory).expect("create temporary directory");
        let store = MemorySecretStore::default();
        let mut invalid = input();
        invalid.api_key.clear();

        let error = configure_provider(&store, &directory, invalid).unwrap_err();

        assert_eq!(error, "DESKTOP_PROVIDER_CONFIGURATION_INVALID");
        assert!(store.get(API_KEY_ACCOUNT).unwrap().is_none());
        assert!(!directory.join(CONFIG_FILE).exists());
        std::fs::remove_dir_all(directory).expect("remove temporary directory");
    }

    #[test]
    fn noncanonical_model_is_rejected_before_any_secret_is_written() {
        let directory = temporary_directory("noncanonical-model");
        std::fs::create_dir_all(&directory).expect("create temporary directory");
        let store = MemorySecretStore::default();
        let mut invalid = input();
        invalid.vision_model = "glm-4.6v".to_string();

        let error = configure_provider(&store, &directory, invalid).unwrap_err();

        assert_eq!(error, "DESKTOP_PROVIDER_CONFIGURATION_INVALID");
        assert!(store.get(API_KEY_ACCOUNT).unwrap().is_none());
        assert!(!directory.join(CONFIG_FILE).exists());
        std::fs::remove_dir_all(directory).expect("remove temporary directory");
    }

    #[test]
    fn legacy_model_configuration_requires_revalidation() {
        let directory = temporary_directory("legacy-model");
        std::fs::create_dir_all(&directory).expect("create temporary directory");
        let store = MemorySecretStore::default();
        store
            .set(API_KEY_ACCOUNT, b"synthetic-test-key-value")
            .expect("store key");
        store
            .set(PII_PEPPER_ACCOUNT, b"0123456789abcdef0123456789abcdef")
            .expect("store pepper");
        write_configuration(
            &directory,
            &ProviderConfigurationFile {
                provider: "zhipu".to_string(),
                text_model: "glm-5.2".to_string(),
                vision_model: "glm-4.6v".to_string(),
                base_url: ZHIPU_BASE_URL.to_string(),
                validated: true,
            },
        )
        .expect("write legacy config");

        let status = provider_status(&store, &directory).expect("provider status");

        assert!(status.configured);
        assert!(!status.validated);
        assert_eq!(status.text_model, "glm-5.3-flash");
        assert_eq!(status.vision_model, "glm-5.3-flash");
        assert!(provider_environment(&store, &directory)
            .expect("provider environment")
            .is_empty());
        std::fs::remove_dir_all(directory).expect("remove temporary directory");
    }
}
