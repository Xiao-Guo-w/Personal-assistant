"""
查询改写：多轮对话里的指代消解。

解决什么问题：用户追问"那它呢""这个多少钱"时，检索语句本身不含任何
有效信息，直接拿去检索必然跑偏。先用模型把最近几轮对话揉成一句
自包含的检索语句，再去检索。

三条克制原则（不做的话，这个功能很容易帮倒忙）：

1. **条件触发**。只在检测到代词、省略或极短 query 时才调用模型。
   首轮提问、本身已经自包含的问题直接跳过，省一次调用和几秒延迟。

2. **改写结果与原始 query 一起检索，不替换**。改写会引入幻觉 ——
   用户问"退款要几天"，模型可能改写成"退款到账时间及手续费"，
   凭空多出"手续费"。只用改写结果检索就被污染了。两路一起送入
   RRF 融合，既消解了指代，又保住了用户原话。

3. **失败一律降级**。超时、调用报错、模型不听话，都退回原始 query，
   绝不因为改写失败而让检索整体不可用。
"""

import asyncio
import logging

from langchain_core.messages import HumanMessage, SystemMessage

from ..config import settings

logger = logging.getLogger(__name__)

# 出现这些词说明检索语句依赖上文
_REFERENCE_MARKERS = (
    "它", "他", "她", "这个", "那个", "这些", "那些", "该", "此", "其",
    "上面", "上述", "刚才", "之前", "前面", "同样", "还有", "另外", "呢",
)

# 很短的问题大概率是省略句
_SHORT_QUERY_LEN = 8

_NO_CHANGE = "NO_CHANGE"

_REWRITE_PROMPT = f"""你在为知识库检索做查询改写。

把用户最新的问题改写成一句**自包含**的检索语句：把代词、省略指代的内容补全，
让它脱离对话也能读懂。要求：

1. 只输出改写后的检索语句本身，不要解释、不要引号、不要前后缀。
2. 忠实于用户原意，**不要添加原文没有的条件**（例如原句没提"手续费"就不要补上）。
3. 如果最新问题本身已经完整、不需要补充上下文，只输出 {_NO_CHANGE}。
4. 检索语句用陈述式短语，不要把问题原样抄成疑问句。"""


def needs_rewrite(query: str) -> bool:
    """判断这条 query 是否值得花一次模型调用去改写。"""
    text = (query or "").strip()
    if not text:
        return False
    if len(text) <= _SHORT_QUERY_LEN:
        return True
    return any(marker in text for marker in _REFERENCE_MARKERS)


async def rewrite_query(query: str, history: str) -> str | None:
    """
    改写检索语句。

    返回改写结果；不需要改写、或任何环节出问题时返回 None
    （调用方据此退回原始 query）。
    """
    if not settings.kb_enabled or not settings.kb_query_rewrite:
        return None

    text = (query or "").strip()
    if not text or not needs_rewrite(text):
        return None

    # 没有上下文就没什么可消解的
    if not (history or "").strip():
        return None

    from ..llm import get_llm

    messages = [
        SystemMessage(content=_REWRITE_PROMPT),
        HumanMessage(
            content=f"最近的对话：\n{history}\n\n用户最新的问题：{text}\n\n改写后的检索语句："
        ),
    ]

    try:
        result = await asyncio.wait_for(
            get_llm().ainvoke(messages),
            timeout=settings.kb_rewrite_timeout_seconds,
        )
    except asyncio.TimeoutError:
        logger.warning("查询改写超时，改用原始 query")
        return None
    except Exception:  # noqa: BLE001
        logger.exception("查询改写失败，改用原始 query")
        return None

    content = result.content if isinstance(result.content, str) else str(result.content)
    rewritten = content.strip().strip('"').strip("“”").strip()

    if not rewritten or _NO_CHANGE in rewritten:
        return None
    # 模型有时会把整段解释写出来，取第一行更稳
    rewritten = rewritten.splitlines()[0].strip()
    if not rewritten or rewritten == text:
        return None
    # 改写结果离谱地长，多半是模型没按要求输出，宁可不改
    if len(rewritten) > max(60, len(text) * 4):
        logger.warning("查询改写结果异常，改用原始 query：%r", rewritten[:80])
        return None

    return rewritten
