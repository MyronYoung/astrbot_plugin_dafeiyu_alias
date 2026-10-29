"""QQ 称呼映射：把 QQ 号固定绑定到一个称呼，并在群聊里持续使用。

- 同一 QQ 再次设置会覆盖旧称呼；
- 每次 LLM 请求自动注入称呼表 + 当前发送者称呼；
- 主人可用 `/称呼` 命令或自然语言让模型调用 `set_alias`；
- 纯本地 JSON，不调用 LLM，零 token 额外开销（除注入的称呼表本身）。
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.star_tools import StarTools


@register("astrbot_plugin_dafeiyu_alias", "dafeiyu", "QQ 称呼映射", "1.1.1")
class QQAliasPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.conf = config or {}
        self.enabled = bool(self.conf.get("enabled", True))
        raw_owners = self.conf.get("owner_ids") or ""
        if isinstance(raw_owners, str):
            raw_owners = re.split(r"[,;\s]+", raw_owners)
        self.owner_ids = [str(x).strip() for x in raw_owners if str(x).strip()]
        if not self.owner_ids:
            logger.warning("[alias] 未配置 owner_ids，称呼设置/删除命令不可用")
        try:
            self.max_prompt = int(self.conf.get("max_prompt_aliases", 30))
        except Exception:
            self.max_prompt = 30
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_dafeiyu_alias")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "aliases.json"
        self.aliases: dict[str, dict] = {}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text("utf-8"))
                if isinstance(data, dict):
                    self.aliases = data
            except Exception as e:
                logger.error(f"[alias] 读取称呼映射失败: {e}")

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.aliases, ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, self.path)

    def _is_owner(self, event) -> bool:
        try:
            return str(event.get_sender_id()) in self.owner_ids
        except Exception:
            return False

    @staticmethod
    def _norm_qq(qq: str) -> str:
        q = re.sub(r"\D", "", str(qq or ""))
        return q if re.fullmatch(r"\d{5,12}", q) else ""

    def _set(self, qq: str, alias: str, relation: str = "") -> tuple[bool, str]:
        q = self._norm_qq(qq)
        name = str(alias or "").strip()[:30]
        rel = str(relation or "").strip()[:20]
        if not q:
            return False, "QQ 号格式不对，需要 5-12 位数字。"
        if not name:
            return False, "称呼不能为空。"
        old = self.aliases.get(q, {})
        self.aliases[q] = {
            "alias": name,
            "relation": rel,
            "updated": datetime.now().isoformat(timespec="seconds"),
            "created": old.get("created") or datetime.now().isoformat(timespec="seconds"),
        }
        self._save()
        suffix = f"（{rel}）" if rel else ""
        action = "已更新" if old else "已记住"
        return True, f"{action}：QQ {q} → {name}{suffix}"

    @filter.on_llm_request()
    async def inject_aliases(self, event, req):
        if not self.enabled or not self.aliases:
            return
        try:
            items = list(self.aliases.items())[: self.max_prompt]
            lines = []
            for qq, rec in items:
                alias = str(rec.get("alias", "")).strip()
                if not alias:
                    continue
                rel = str(rec.get("relation", "")).strip()
                lines.append(f"- QQ {qq} = {alias}" + (f"（{rel}）" if rel else ""))
            if not lines:
                return
            sender = str(event.get_sender_id() or "")
            current = ""
            if sender in self.aliases:
                rec = self.aliases[sender]
                current = str(rec.get("alias", "")).strip()
            block = "\n【QQ 称呼表（固定映射，按此称呼，不要叫错）】\n" + "\n".join(lines)
            if current:
                block += f"\n当前发送者 QQ：{sender}，称呼：{current}；请直接用这个称呼。"
            req.system_prompt += block + "\n"
        except Exception as e:
            logger.debug(f"[alias] 注入称呼表失败: {e}")

    def _parse_mappings(self, text: str) -> str:
        chunks = [c.strip() for c in re.split(r"[;；\n]+", text or "") if c.strip()]
        if not chunks:
            return "没有解析到映射。格式：QQ号=称呼/关系;QQ号=称呼/关系"
        ok_msgs, errors = [], []
        for chunk in chunks[:50]:
            m = re.match(r"^(?:QQ|qq)?\s*(\d{5,12})\s*[=:：,，\s]\s*(.+)$", chunk)
            if not m:
                errors.append(f"{chunk}（格式不对）")
                continue
            qq, rest = m.group(1), m.group(2).strip()
            relation = ""
            for sep in ("/", "|"):
                if sep in rest:
                    alias, relation = rest.split(sep, 1)
                    break
            else:
                parts = rest.split(None, 1)
                alias = parts[0]
                relation = parts[1] if len(parts) > 1 else ""
            ok, msg = self._set(qq, alias.strip(), relation.strip())
            (ok_msgs if ok else errors).append(msg)
        lines = []
        if ok_msgs:
            lines.append(f"已设置 {len(ok_msgs)} 条：")
            lines.extend(f"- {m}" for m in ok_msgs)
        if errors:
            lines.append("失败：")
            lines.extend(f"- {e}" for e in errors)
        return "\n".join(lines) or "没有可设置的映射。"

    @filter.llm_tool(name="set_aliases")
    async def set_aliases_tool(self, event, mappings: str) -> str:
        """批量设置多个 QQ 号到称呼的固定映射；同一 QQ 会覆盖旧称呼。

        多条映射请放在一个字符串里，用分号或换行分隔，每条格式：
        QQ号=称呼/关系，或 QQ号=称呼。例如：
        123456=张哥/哥哥;654321=李四/群友

        Args:
            mappings(string): 多条映射文本，分号/换行分隔。
        """
        if not self._is_owner(event):
            return "只有主人能设置称呼。"
        return self._parse_mappings(mappings)

    @filter.llm_tool(name="set_alias")
    async def set_alias_tool(self, event, qq: str, alias: str, relation: str = "") -> str:
        """把 QQ 号和固定称呼绑定；同一 QQ 再次设置会覆盖旧称呼。

        Args:
            qq(string): QQ 号，5-12 位数字。
            alias(string): 应该固定使用的称呼，如“张哥”“主人”“竹鼠哥哥”。
            relation(string): 可选关系备注，如“哥哥”“群友”“主人”。
        """
        if not self._is_owner(event):
            return "只有主人能设置称呼。"
        ok, msg = self._set(qq, alias, relation)
        return msg

    @filter.command("称呼")
    async def set_cmd(self, event):
        if not self._is_owner(event):
            yield event.plain_result("只有主人能设置称呼。")
            return
        parts = (event.message_str or "").split()
        if len(parts) < 3:
            yield event.plain_result("用法：/称呼 QQ号 称呼 [关系]，例如 /称呼 123456 张哥 哥哥")
            return
        qq, alias = parts[1], parts[2]
        relation = " ".join(parts[3:]) if len(parts) > 3 else ""
        ok, msg = self._set(qq, alias, relation)
        yield event.plain_result(msg)

    @filter.command("称呼批量")
    async def batch_cmd(self, event):
        if not self._is_owner(event):
            yield event.plain_result("只有主人能设置称呼。")
            return
        text = (event.message_str or "").split(maxsplit=1)
        if len(text) < 2:
            yield event.plain_result("用法：/称呼批量 123456=张哥/哥哥;654321=李四/群友")
            return
        yield event.plain_result(self._parse_mappings(text[1]))

    @filter.command("称呼列表")
    async def list_cmd(self, event):
        if not self._is_owner(event):
            yield event.plain_result("只有主人能查看称呼表。")
            return
        if not self.aliases:
            yield event.plain_result("称呼表还是空的。")
            return
        lines = ["QQ 称呼表："]
        for qq, rec in list(self.aliases.items())[:50]:
            rel = str(rec.get("relation", "")).strip()
            lines.append(f"- {qq} → {rec.get('alias','')}" + (f"（{rel}）" if rel else ""))
        yield event.plain_result("\n".join(lines))

    @filter.command("称呼删除")
    async def delete_cmd(self, event):
        if not self._is_owner(event):
            yield event.plain_result("只有主人能删除称呼。")
            return
        parts = (event.message_str or "").split()
        if len(parts) < 2:
            yield event.plain_result("用法：/称呼删除 QQ号")
            return
        q = self._norm_qq(parts[1])
        if not q:
            yield event.plain_result("QQ 号格式不对。")
            return
        if q in self.aliases:
            self.aliases.pop(q, None)
            self._save()
            yield event.plain_result(f"已删除 QQ {q} 的称呼。")
        else:
            yield event.plain_result(f"没有 QQ {q} 的称呼记录。")
