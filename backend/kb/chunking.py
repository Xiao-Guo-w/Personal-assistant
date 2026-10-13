"""
分块：结构优先，超长段落再按 token 切。

为什么不用"纯按 token 硬切"：
一句话被劈成两半之后，两个片段各自都读不通，检索命中率会明显下降。
所以先按标题 / 空行把文档还原成段落，段落超长才退化成按句子切。

每个块还会带上"标题路径"（如 `产品手册 > 3. 计费说明 > 3.2 超额规则`）：
片段脱离原文后经常看不出自己在讲什么，带上路径能明显提升召回与答案质量。

token 计数做成可注入：便于在没有 tiktoken 的环境里单独测试分块算法。
"""

import re
from dataclasses import dataclass
from typing import Callable

# Markdown 标题：## 标题
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
# 句子边界（中英文）。用 lookbehind 保留句末标点在前一段
_SENTENCE_RE = re.compile(r"(?<=[。！？!?；;])")
# 零宽字符 / BOM，解析出来会干扰查重与检索
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u2028-\u202f\ufeff]")

_encoder = None


def count_tokens(text: str) -> int:
    """默认 token 计数器（tiktoken cl100k_base，与上下文压缩层保持一致）。"""
    global _encoder
    if _encoder is None:
        import tiktoken

        _encoder = tiktoken.get_encoding("cl100k_base")
    if not text:
        return 0
    return len(_encoder.encode(text))


@dataclass
class Block:
    """一个段落（可能带标题路径）。"""

    text: str
    heading: str = ""


@dataclass
class Chunk:
    """最终入库的分块。"""

    text: str
    token_count: int


def normalize_text(raw: str) -> str:
    """
    文本规范化。

    这一步必须在计算 content_hash 之前做，否则同一份内容以不同换行符
    （\\r\\n / \\r）解析出来会算出不同的 hash，查重直接失效。
    """
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = _INVISIBLE_RE.sub("", text)
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines)
    # 连续 3 个以上空行压成 2 个：保留段落边界，但去掉大段空白
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_blocks(text: str) -> list[Block]:
    """按标题与空行切段，并给每段带上标题路径。"""
    heading_stack: list[tuple[int, str]] = []
    blocks: list[Block] = []
    buf: list[str] = []

    def flush() -> None:
        if not buf:
            return
        body = "\n".join(buf).strip()
        buf.clear()
        if body:
            blocks.append(Block(text=body, heading=_heading_path(heading_stack)))

    for line in text.split("\n"):
        match = _HEADING_RE.match(line)
        if match:
            flush()
            level = len(match.group(1))
            title = match.group(2).strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            continue

        if not line.strip():
            flush()
            continue

        buf.append(line)

    flush()
    return blocks


def _heading_path(stack: list[tuple[int, str]]) -> str:
    return " > ".join(title for _, title in stack if title)


def _split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE_RE.split(text)]
    return [p for p in parts if p]


def _explode(
    blocks: list[Block],
    chunk_size: int,
    count: Callable[[str], int],
) -> list[Block]:
    """
    把超长段落拆成不超过 chunk_size 的段落。

    先按句子打包；单个句子本身就超长（例如一整段没有标点）时，
    再退化成按字符二分，保证不会产出超长块。
    """
    out: list[Block] = []
    for block in blocks:
        if count(block.text) <= chunk_size:
            out.append(block)
            continue

        piece: list[str] = []
        piece_tokens = 0
        for sentence in _split_sentences(block.text):
            st = count(sentence)
            if st > chunk_size:
                # 单句超长：先把已有片段收尾，再按字符硬切
                if piece:
                    out.append(Block(text="".join(piece), heading=block.heading))
                    piece, piece_tokens = [], 0
                out.extend(
                    Block(text=part, heading=block.heading)
                    for part in _hard_split(sentence, chunk_size, count)
                )
                continue

            if piece and piece_tokens + st > chunk_size:
                out.append(Block(text="".join(piece), heading=block.heading))
                piece, piece_tokens = [], 0
            piece.append(sentence)
            piece_tokens += st

        if piece:
            out.append(Block(text="".join(piece), heading=block.heading))

    return out


def _hard_split(text: str, chunk_size: int, count: Callable[[str], int]) -> list[str]:
    """按字符二分，切到每片都不超预算。"""
    if count(text) <= chunk_size:
        return [text]

    mid = len(text) // 2
    left, right = text[:mid], text[mid:]
    if not left or not right:
        return [text]
    return _hard_split(left, chunk_size, count) + _hard_split(right, chunk_size, count)


def _tail(blocks: list[Block], overlap: int, count: Callable[[str], int]) -> list[Block]:
    """取尾部若干 token 的段落，作为下一块的重叠部分。"""
    if overlap <= 0:
        return []
    tail: list[Block] = []
    total = 0
    for block in reversed(blocks):
        bt = count(block.text)
        if tail and total + bt > overlap:
            break
        tail.insert(0, block)
        total += bt
    # 整块都被当作重叠时说明这一块整体都很短，全留即可
    return tail


def build_chunks(
    blocks: list[Block],
    *,
    chunk_size: int = 500,
    overlap: int = 80,
    min_tokens: int = 40,
    count: Callable[[str], int] = count_tokens,
) -> list[Chunk]:
    """
    把段落打包成块。

    贪心填充到 chunk_size 就收尾，并用尾部若干 token 作为下一块的重叠，
    避免"答案正好被切在边界上"时两条命中的块都答不完整。
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须大于 0")
    overlap = max(0, min(overlap, chunk_size // 2))
    min_tokens = max(0, min(min_tokens, chunk_size))

    pieces = _explode(blocks, chunk_size, count)
    chunks: list[Chunk] = []
    current: list[Block] = []
    current_tokens = 0
    current_heading = ""
    # 上一块消费掉的段落数，用来判断收尾时是否有新内容，避免产出纯重叠块
    pending = 0

    def emit() -> None:
        nonlocal current, current_tokens, current_heading, pending
        if not current or pending == 0:
            return
        body = "\n\n".join(b.text for b in current).strip()
        text = f"{current_heading}\n{body}" if current_heading else body
        if text:
            chunks.append(Chunk(text=text, token_count=count(text)))
        current = _tail(current, overlap, count)
        current_tokens = sum(count(b.text) for b in current)
        current_heading = current[0].heading if current else ""
        pending = 0

    for block in pieces:
        heading = block.heading
        # 标题前缀会拼进正文，预算里要先把它的位置留出来，
        # 否则产出的块会比 chunk_size 大出一截
        reserve = count(heading) + 1 if heading else 0
        limit = max(64, chunk_size - reserve)
        bt = count(block.text)

        if current and (
            current_tokens + bt > limit
            # 跨章节就换块：否则标题路径会标错，片段被挂到别的章节名下
            or (heading != current_heading and current_tokens >= min_tokens)
        ):
            emit()
            # 重叠部分自己就占满了预算 —— 此时 emit 不会再产出新块，
            # 必须丢掉重叠，否则继续追加就会超出 chunk_size
            if current_tokens + bt > limit:
                current, current_tokens, current_heading = [], 0, ""

        if not current:
            current_heading = heading
        current.append(block)
        current_tokens += bt
        pending += 1

    emit()

    # 过短的块并入前一块，避免索引里塞满无意义的碎片。
    #
    # 这一步会让合并后的块超过 chunk_size，上限是 chunk_size + min_tokens。
    # 这是有意的取舍：碎片块检索出来几乎没用（一句话撑不起答案），
    # 而略大一点的块只是多占一点上下文预算，后者可控、前者不可控。
    # 真正需要严守的总量上限由 search 层的 kb_token_budget 兜底。
    merged: list[Chunk] = []
    for chunk in chunks:
        if merged and chunk.token_count < min_tokens:
            previous = merged[-1]
            text = f"{previous.text}\n\n{chunk.text}"
            merged[-1] = Chunk(text=text, token_count=count(text))
        else:
            merged.append(chunk)
    return merged


def chunk_text(
    text: str,
    *,
    chunk_size: int = 500,
    overlap: int = 80,
    min_tokens: int = 40,
    count: Callable[[str], int] = count_tokens,
) -> list[Chunk]:
    """文本 → 分块。入库管线直接调这个。"""
    normalized = normalize_text(text)
    if not normalized:
        return []
    return build_chunks(
        split_blocks(normalized),
        chunk_size=chunk_size,
        overlap=overlap,
        min_tokens=min_tokens,
        count=count,
    )
