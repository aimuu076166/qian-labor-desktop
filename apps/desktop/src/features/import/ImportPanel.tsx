import { useState } from 'react';
import { selectEmploymentFiles } from '../../lib/desktop';

type ImportPanelProps = {
  onSelected: (paths: string[]) => void | Promise<void>;
};

export function ImportPanel({ onSelected }: ImportPanelProps) {
  const [isSelecting, setIsSelecting] = useState(false);

  async function handleSelect() {
    if (isSelecting) return;
    setIsSelecting(true);
    try {
      const paths = await selectEmploymentFiles();
      if (paths.length > 0) await onSelected(paths);
    } finally {
      setIsSelecting(false);
    }
  }

  return (
    <section className="import-panel" aria-labelledby="import-title">
      <div>
        <p className="eyebrow">本机材料导入</p>
        <h2 id="import-title">一次选择企业现有材料</h2>
        <p className="muted">支持 Excel、Word、PDF、图片和扫描件，系统将在本机自动分流处理。</p>
      </div>
      <button type="button" className="primary-action" disabled={isSelecting} onClick={handleSelect}>
        {isSelecting ? '正在导入…' : '选择企业材料'}
      </button>
    </section>
  );
}
