#!/usr/bin/env bash
# Подготовка инструментов для Ubuntu 24.04. Запуск от обычного пользователя с sudo.
set -euo pipefail

usage() {
    echo 'bash prepare-vm.sh           — установить зависимости и скачать открытые модели'
    echo 'bash prepare-vm.sh --check   — проверить установку без скачивания'
}

if (( $# > 1 )); then usage >&2; exit 2; fi
case "${1:-}" in
    "") mode=install ;;
    --check) mode=check ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
esac

if [[ ! -r /etc/os-release ]]; then echo 'Нужна Ubuntu 24.04.' >&2; exit 1; fi
. /etc/os-release
if [[ "$ID" != ubuntu || "$VERSION_ID" != 24.04 ]]; then
    echo 'Нужна Ubuntu 24.04.' >&2; exit 1
fi
if (( EUID == 0 )); then
    echo 'Запустите скрипт из своего SSH-сеанса без sudo. Для системных команд он вызовет sudo сам.' >&2
    exit 1
fi

venv="$HOME/.local/share/hackathon-tools/venv"
cache="${FASTEMBED_CACHE_PATH:-$HOME/.cache/fastembed}"
e5_dir="${AI_MECHANIC_E5_DIR:-$HOME/.cache/ai-mechanic/e5-small}"
image=qdrant/qdrant:v1.19.2

models() {
    AI_MECHANIC_E5_DIR="$e5_dir" "$venv/bin/python" - "$cache" "$1" <<'PY'
import os
import sys
from hashlib import file_digest
from pathlib import Path

import numpy as np
import onnxruntime as ort
from fastembed import SparseTextEmbedding
from huggingface_hub import hf_hub_download
from tokenizers import Tokenizer

cache, mode = sys.argv[1:]
directory = Path(os.environ["AI_MECHANIC_E5_DIR"])
revision = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
files = {
    "tokenizer.json": "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
    "onnx/model.onnx": "ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665",
}


def verified(path, checksum):
    if not path.is_file():
        return False
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest() == checksum


for name, checksum in files.items():
    path = directory / name
    if verified(path, checksum):
        continue
    if mode == "offline":
        sys.exit(f"Проверьте файл E5 Small: {path}")
    hf_hub_download("intfloat/multilingual-e5-small", name, revision=revision,
                    local_dir=directory, force_download=True)
    if not verified(path, checksum):
        sys.exit(f"Контрольная сумма файла E5 Small не совпала: {path}")

marker = directory / "REVISION"
if not marker.is_file() or marker.read_text().strip() != revision:
    if mode == "offline":
        sys.exit(f"Проверьте ревизию E5 Small: {marker}")
    marker.write_text(revision + "\n", encoding="ascii")

sample = "проверка двигателя"
tokens = Tokenizer.from_file(str(directory / "tokenizer.json")).encode("query: " + sample)
ids = np.asarray([tokens.ids], dtype=np.int64)
options = ort.SessionOptions()
options.intra_op_num_threads = 2
graph = ort.InferenceSession(str(directory / "onnx/model.onnx"), sess_options=options,
                             providers=["CPUExecutionProvider"])
hidden, = graph.run(["last_hidden_state"], {"input_ids": ids, "attention_mask": np.ones_like(ids),
                                            "token_type_ids": np.zeros_like(ids)})
sparse = SparseTextEmbedding("Qdrant/bm25", language="russian", cache_dir=cache, threads=2,
                             local_files_only=(mode == "offline"))
if hidden.shape != (1, ids.shape[1], 384) or not np.isfinite(hidden).all() or not len(next(sparse.query_embed(sample)).indices):
    sys.exit("Ошибка проверки E5 Small или BM25.")
print("E5 Small и русский BM25 готовы.")
PY
}

if [[ "$mode" == install ]]; then
    sudo -v
    packages=()
    for package in ca-certificates git python3 python3-venv tesseract-ocr tesseract-ocr-rus; do
        if [[ "$(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null)" != installed ]]; then
            packages+=("$package")
        fi
    done
    if ! command -v docker >/dev/null; then packages+=(docker.io); fi
    if ! command -v caddy >/dev/null; then
        packages+=(caddy)
    fi
    if (( ${#packages[@]} )); then
        sudo apt-get -o DPkg::Lock::Timeout=180 update
        # Блокируем запуск Caddy с типовой конфигурацией до подготовки стенда.
        if ! command -v caddy >/dev/null && ! systemctl cat caddy.service >/dev/null 2>&1; then
            sudo systemctl mask caddy.service
        fi
        sudo env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=180 install -y --no-install-recommends "${packages[@]}"
    fi
    sudo systemctl start docker
    if ! sudo docker image inspect "$image" >/dev/null 2>&1; then
        sudo docker pull "$image"
    fi

    mkdir -p "$(dirname "$venv")" "$cache"
    if ! "$venv/bin/python" -m pip --version >/dev/null 2>&1; then
        python3 -m venv "$venv"
    fi
    "$venv/bin/python" -m pip install 'PyMuPDF==1.26.7' 'filelock>=3.13,<4' 'qdrant-client==1.20.0' 'fastembed==0.9.1'
fi

git --version
caddy version
sudo docker image inspect --format '{{.Id}}' "$image"
languages=$(tesseract --list-langs)
if ! grep -Fxq 'rus' <<< "$languages"; then echo 'Нет русского языка для Tesseract.' >&2; exit 1; fi
if [[ ! -x "$venv/bin/python" ]]; then echo 'Окружение Python ещё не установлено.' >&2; exit 1; fi
"$venv/bin/python" -m pip check
"$venv/bin/python" -c 'import pymupdf, filelock, qdrant_client, fastembed'
if [[ "$mode" == check ]]; then
    HF_HUB_OFFLINE=1 models offline
elif ! HF_HUB_OFFLINE=1 models offline 2>/dev/null; then
    models download
fi

printf 'Проверка пройдена.\nPython: %s/bin/python\nКэш BM25: %s\nE5 Small: %s\n' "$venv" "$cache" "$e5_dir"
