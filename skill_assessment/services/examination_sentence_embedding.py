# route: (examination) | file: skill_assessment/services/examination_sentence_embedding.py
"""
Ленивая загрузка sentence-transformers для семантического сравнения ответа с регламентом.
При отсутствии библиотеки или ошибке — возвращается ``None`` (используется эвристика).
"""

from __future__ import annotations

import logging
import os
import threading

from skill_assessment.domain.examination_scoring import TextEmbedder

_log = logging.getLogger(__name__)
_lock = threading.Lock()
_embedder: TextEmbedder | None = None
_load_attempted: bool = False


def _get_model_name() -> str:
    return (os.getenv("SKILL_ASSESSMENT_ST_MODEL") or "paraphrase-multilingual-MiniLM-L12-v2").strip()


def get_sentence_transformer_embedder() -> TextEmbedder | None:
    """Один экземпляр на процесс; ``None`` если отключено или пакет недоступен."""
    global _embedder, _load_attempted
    if os.getenv("SKILL_ASSESSMENT_DISABLE_SEMANTIC_SCORING", "").strip().lower() in ("1", "true", "yes"):
        return None
    with _lock:
        if _load_attempted:
            return _embedder
        _load_attempted = True
        try:
            from sentence_transformers import SentenceTransformer
        except Exception as e:
            _log.debug("sentence-transformers недоступен: %s", e)
            _embedder = None
            return None

        class _STEmbedder:
            def __init__(self, model_name: str) -> None:
                self._model = SentenceTransformer(model_name)

            def embed(self, text: str) -> list[float]:
                v = self._model.encode(text or "", normalize_embeddings=True)
                return v.tolist()

        try:
            _embedder = _STEmbedder(_get_model_name())
            _log.info("sentence-transformers: модель загружена (%s)", _get_model_name())
        except Exception:
            _log.exception("sentence-transformers: не удалось загрузить модель")
            _embedder = None
        return _embedder
