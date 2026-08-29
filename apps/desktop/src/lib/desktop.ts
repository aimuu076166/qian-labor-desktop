import { open } from '@tauri-apps/plugin-dialog';

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
