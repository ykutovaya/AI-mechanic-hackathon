"""Кодирование фрагментов руководств и вопросов механика моделью E5 Small."""

from importlib.metadata import version
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

from qdrant_client import models

E5_MODEL = "intfloat/multilingual-e5-small"
E5_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
BM25_PARAMETERS = {"k": 1.2, "b": 0.75, "avg_len": 256.0}


def _sparse(vector):
    return models.SparseVector(indices=vector.indices.tolist(), values=vector.values.tolist())


class E5Embeddings:
    """Кодирует фрагменты и запросы разными префиксами, сохраняя исходный BM25."""

    def __init__(self, model_dir, sparse, *, revision, threads=4, batch_size=8,
                 onnx_file="onnx/model.onnx"):
        directory = Path(model_dir)
        if batch_size < 1 or threads < 1:
            raise ValueError("Размер пакета и число потоков должны быть положительными.")
        self.batch_size = batch_size
        self.sparse = sparse
        self.tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self.pad_id = self.tokenizer.token_to_id("<pad>")
        self.start_id, self.end_id = self.tokenizer.encode("").ids
        self.signature = "|".join((E5_MODEL, str(revision), onnx_file,
                                   "prefixed-512-weighted-v1", version("onnxruntime"),
                                   version("tokenizers"), "Qdrant/bm25:russian", version("fastembed"),
                                   *(f"{key}={value}" for key, value in BM25_PARAMETERS.items())))
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(directory / onnx_file), sess_options=options,
                                            providers=["CPUExecutionProvider"])
        self.inputs = {node.name for node in self.session.get_inputs()}

    def _batch(self, sequences):
        width = max(map(len, sequences))
        ids = np.full((len(sequences), width), self.pad_id, dtype=np.int64)
        mask = np.zeros_like(ids)
        for row, sequence in enumerate(sequences):
            ids[row, :len(sequence)] = sequence
            mask[row, :len(sequence)] = 1
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden, = self.session.run(["last_hidden_state"], feed)
        means = np.einsum("bth,bt->bh", hidden, mask.astype(np.float32)) / mask.sum(1)[:, None]
        return means / np.linalg.norm(means, axis=1, keepdims=True)

    def dense_vectors(self, texts, *, prefix="passage: "):
        """Повторяет префикс в каждом окне и учитывает число токенов текста в окне."""
        if prefix not in ("passage: ", "query: "):
            raise ValueError("Допустимые префиксы E5 — passage и query.")
        prefix_ids = self.tokenizer.encode(prefix.rstrip(), add_special_tokens=False).ids
        room = 512 - len(prefix_ids) - 2
        windows = []
        combined = []
        for row, text in enumerate(texts):
            ids = self.tokenizer.encode(prefix + text, add_special_tokens=False).ids
            if ids[:len(prefix_ids)] != prefix_ids:
                raise ValueError("Токенизатор изменил границу префикса E5.")
            body = ids[len(prefix_ids):]
            combined.append(np.zeros(384, dtype=np.float64))
            for start in range(0, max(1, len(body)), room):
                part = body[start:start + room]
                window = [self.start_id, *prefix_ids, *part, self.end_id]
                windows.append((row, max(1, len(part)), window))

        # Группировка по длине уменьшает заполнение без изменения порядка результатов.
        windows.sort(key=lambda item: len(item[2]), reverse=True)
        for start in range(0, len(windows), self.batch_size):
            batch = windows[start:start + self.batch_size]
            means = self._batch([tokens for _, _, tokens in batch])
            for (row, weight, _), vector in zip(batch, means, strict=True):
                combined[row] += weight * vector
        for vector in combined:
            yield (vector / np.linalg.norm(vector)).astype(np.float32)

    def vectors(self, texts):
        texts = list(texts)
        for dense, sparse in zip(self.dense_vectors(texts), self.sparse.passage_embed(texts), strict=True):
            yield dense.tolist(), _sparse(sparse)

    def query(self, text, hybrid):
        dense = next(self.dense_vectors([text], prefix="query: ")).tolist()
        sparse = _sparse(next(iter(self.sparse.query_embed(text)))) if hybrid else None
        return dense, sparse
