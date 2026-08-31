import { invoke } from '@tauri-apps/api/core';
import { open } from '@tauri-apps/plugin-dialog';
import type {
  ProviderConfigurationInput,
  ProviderConfigurationStatus,
} from '../features/settings/SettingsView';

const extensions = ['csv', 'xls', 'xlsx', 'docx', 'pdf', 'png', 'jpg', 'jpeg', 'webp'];

export async function selectEmploymentFiles(): Promise<string[]> {
  const selected = await open({
    multiple: true,
    directory: false,
    filters: [{ name: '企业劳动用工材料', extensions }],
  });
  if (!selected) return [];
  return Array.isArray(selected) ? selected : [selected];
}

export async function getProviderConfigurationStatus(): Promise<ProviderConfigurationStatus> {
  return invoke<ProviderConfigurationStatus>('provider_configuration_status');
}

export async function configureZhipuProvider(
  input: ProviderConfigurationInput,
): Promise<ProviderConfigurationStatus> {
  return invoke<ProviderConfigurationStatus>('configure_zhipu_provider', { input });
}

export async function markZhipuProviderValidated(): Promise<ProviderConfigurationStatus> {
  return invoke<ProviderConfigurationStatus>('mark_zhipu_provider_validated');
}
