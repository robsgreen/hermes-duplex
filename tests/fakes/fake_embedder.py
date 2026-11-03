"""A deterministic stand-in for the model2vec embedding model, so router tests never download one."""

import re
import zlib
from collections.abc import Sequence

import numpy as np
import numpy.typing as npt

DIMENSIONS = 4096


def fake_embed(texts: Sequence[str]) -> npt.NDArray[np.float64]:
    """Embed each text as a bag of its lowercase words, one hashed dimension per word.

    Texts sharing more words get a higher cosine; a text with no words embeds as all zeros.
    """
    vectors = np.zeros((len(texts), DIMENSIONS))
    for row, text in enumerate(texts):
        for word in re.findall(r"[a-z0-9']+", text.lower()):
            vectors[row, zlib.crc32(word.encode()) % DIMENSIONS] += 1.0
    return vectors
