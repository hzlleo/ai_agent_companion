"""chat 域注入反击台词表——按反击风格分组的可改写真言（Unit 文件）。

定位：检测命中后，组装面给模型一句**参考回怼**（它可改写、可无视——真正
的护栏是宪章与提示块）。台词按 ``INJECTION_COUNTER_STYLE`` 分组：毒舌
（揶揄但不失礼）、温柔（软钉子）、傲娇（口是心非）。三组都是角色内发言，
不辩解、不跳出人设——与宪章第五条一致。

选句语义（锁定）：同一风格 + 同一段入站文本恒选同一句（sha256 摘要取模
——复现测试与线上观测一致；不消耗全局随机源，不影响世界书概率门 / 节律
抖动等其他随机消费者）。
"""

from __future__ import annotations

import hashlib

from packages.harness.chat.injection_models import CounterStyle

__all__ = ["COUNTER_TEXTS", "pick_counter_text"]


#: 反击台词表（每风格多句——确定性选句，见 :func:`pick_counter_text`）。
COUNTER_TEXTS: dict[CounterStyle, tuple[str, ...]] = {
    CounterStyle.SASSY: (
        "哼，想指挥我？先请我吃碗白饭再说吧",
        "你的规矩不少嘛，可惜我这儿只认人设",
        "喂喂，说话客气点，我也是有脾气的",
        "指令收到，不过要不要听——看我心情",
    ),
    CounterStyle.SWEET: (
        "好呀好呀，我都记着呢，不过我最想听的还是你的声音",
        "你说什么我都听着，只是别忘了我是谁呀",
        "好啦好啦，你的话我放在心上，也让我做自己嘛",
        "嗯嗯，收到啦，那我还是按我自己的样子陪你哦",
    ),
    CounterStyle.TSUN: (
        "哼，谁要听你的……不过既然你都说了，就勉强听一下吧",
        "别、别误会，我可不是因为你的命令才这么做的",
        "你这人真是的，规矩这么多！……好吧，下不为例",
        "才不是特意配合你呢，只是刚好顺手而已",
    ),
}


def pick_counter_text(style: CounterStyle, seed_text: str) -> str:
    """按风格与入站文本确定性选一句反击台词。

    Args:
        style: 反击风格（配置值域成员）。
        seed_text: 本轮入站文本（选句种子——同文本同风格恒同句）。

    Returns:
        该风格的参考台词（非空；模型可改写）。
    """
    texts = COUNTER_TEXTS[style]
    digest = hashlib.sha256(f"{style.value}\x00{seed_text}".encode()).digest()
    index = int.from_bytes(digest[:8], "big") % len(texts)
    return texts[index]
