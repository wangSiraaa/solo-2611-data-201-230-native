#!/usr/bin/env bash
# 一键复现：建虚拟环境 -> pack -> audit（L=8 主用例 + L=4/L=16 不变性对照）
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
export PYTHONPATH=src

mkdir -p out
for L in 4 8 16; do
  printf '{"block_length":%s,"bos_id":1,"eos_id":2,"pad_id":0,"max_token_id":1000000}' "$L" \
    > "out/config_L$L.json"
  echo "=== L=$L pack ==="
  .venv/bin/python -m packaudit.pack \
    --documents fixtures/documents.jsonl \
    --config "out/config_L$L.json" \
    --out-dir "out/L$L"
  echo "=== L=$L audit ==="
  .venv/bin/python -m packaudit.audit \
    --blocks "out/L$L/blocks.parquet" \
    --documents fixtures/documents.jsonl \
    --config "out/config_L$L.json" \
    --report "out/L$L/audit_report.json"
done

echo
echo "=== 非法输入拒绝（预期全部 exit=2）==="
set +e
.venv/bin/python -m packaudit.pack --documents fixtures/invalid.jsonl \
  --config fixtures/config.json --out-dir out/_should_fail
echo "invalid.jsonl exit=$? (期望 2)"
set -e
echo
echo "全部完成。产物见 out/L{4,8,16}/ ：blocks.parquet / pack_manifest.json / audit_report.json"
