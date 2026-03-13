from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.all import AstrBotConfig
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.message.components import Plain, At
import datetime
import aiohttp
import time

TEABLE_TOKEN = "teable_accgsDeFVAjQLd2aVRL_oBjbm265m9HjhAoe+kfEwLJ82/sx4lhQMex/eaF+S58="
TEABLE_TABLE_ID = "tblyqs2bqCDTWF8NzAK"
TEABLE_API = f"https://app.teable.ai/api/table/{TEABLE_TABLE_ID}/record"

# ── 内存缓存：序号 → 记录信息 ──────────────────────────────────────────────────
_feedback_mapping: dict[int, dict] = {}

# ── 活跃征集：群号 → {topic, expire_time, platform_name} ──────────────────────
_active_collections: dict[str, dict] = {}


def _save_mapping(mapping: dict):
    global _feedback_mapping
    _feedback_mapping = mapping


def _load_mapping() -> dict:
    return _feedback_mapping


# ── 辅助：发私聊给指定QQ ────────────────────────────────────────────────────────
async def _send_private(context: Context, platform_name: str, qq: str, text: str):
    try:
        platforms = context.platform_manager.get_insts()
        if not platforms:
            return
        platform = platforms[0]
        session = MessageSession(
            platform_name=platform_name,
            message_type=MessageType.FRIEND_MESSAGE,
            session_id=qq,
        )
        await platform.send_by_session(session, MessageChain([Plain(text)]))
    except Exception as e:
        logger.warning(f"[FeedbackPlugin] 私聊发送失败: {e}")


# ── 辅助：发群消息（动态群号）──────────────────────────────────────────────────
async def _send_group(context: Context, platform_name: str, group_id: str, text: str):
    try:
        platforms = context.platform_manager.get_insts()
        if not platforms:
            return
        platform = platforms[0]
        session = MessageSession(
            platform_name=platform_name,
            message_type=MessageType.GROUP_MESSAGE,
            session_id=group_id,
        )
        await platform.send_by_session(session, MessageChain([Plain(text)]))
    except Exception as e:
        logger.warning(f"[FeedbackPlugin] 群消息发送失败 (群{group_id}): {e}")


# ── 辅助：更新 Teable 记录字段 ──────────────────────────────────────────────────
async def _update_record(record_id: str, fields: dict, field_key_type: str = "name"):
    async with aiohttp.ClientSession() as session:
        resp = await session.patch(
            f"{TEABLE_API}/{record_id}",
            headers={
                "Authorization": f"Bearer {TEABLE_TOKEN}",
                "Content-Type": "application/json",
            },
            params={"fieldKeyType": field_key_type},
            json={"record": {"fields": fields}},
        )
        if resp.status not in (200, 201):
            body = await resp.text()
            raise Exception(f"Teable PATCH {resp.status}: {body}")


# ── 辅助：根据来源字段决定通知方式 ─────────────────────────────────────────────
async def _notify_submitter(context: Context, platform_name: str, item: dict, text: str):
    source = item.get("source", "")
    submitter_qq = item.get("submitter_qq", "")
    if not submitter_qq:
        return
    if source.startswith("群 "):
        group_id = source.replace("群 ", "").strip()
        try:
            platforms = context.platform_manager.get_insts()
            if not platforms:
                return
            platform = platforms[0]
            session = MessageSession(
                platform_name=platform_name,
                message_type=MessageType.GROUP_MESSAGE,
                session_id=group_id,
            )
            await platform.send_by_session(
                session,
                MessageChain([At(qq=submitter_qq), Plain(f" {text}")])
            )
        except Exception as e:
            logger.warning(f"[FeedbackPlugin] 群消息发送失败 (群{group_id}): {e}")
    else:
        await _send_private(context, platform_name, submitter_qq, text)


@register("feedback_plugin", "Nayuki", "意见反馈插件 - 将用户意见转发到Teable并支持管理员驳回/通过", "3.2.0")
class FeedbackPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        admin1 = str(config.get("admin_qq_1", "")).strip()
        admin2 = str(config.get("admin_qq_2", "")).strip()
        self.admin_ids: list[str] = [x for x in [admin1, admin2] if x]

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        return str(event.get_sender_id()) in self.admin_ids

    # ── /意见 <内容> ────────────────────────────────────────────────────────────
    @filter.command("意见")
    async def feedback(self, event: AstrMessageEvent):
        content = event.message_str.strip()

        if content.startswith("/意见"):
            content = content[len("/意见"):].strip()
        elif content.startswith("意见"):
            content = content[len("意见"):].strip()

        if not content:
            yield event.plain_result("❌ 请在命令后填写你的意见，例如：/意见 希望增加某某功能")
            return

        sender_id = event.get_sender_id()
        sender_name = event.get_sender_name() if hasattr(event, "get_sender_name") else "未知用户"

        source = "私聊"
        try:
            if hasattr(event, "message_obj") and hasattr(event.message_obj, "group_id"):
                gid = event.message_obj.group_id
                if gid:
                    source = f"群 {gid}"
        except Exception:
            pass

        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.post(
                    TEABLE_API,
                    headers={
                        "Authorization": f"Bearer {TEABLE_TOKEN}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "records": [{
                            "fields": {
                                "意见内容": content,
                                "用户ID": str(sender_id),
                                "用户昵称": sender_name,
                                "来源": source,
                                "时间": now,
                                "状态": "待处理",
                            }
                        }]
                    },
                )
                result = await resp.json()
                if resp.status != 201:
                    raise Exception(f"Teable API 返回 {resp.status}: {result}")
            logger.info(f"[FeedbackPlugin] 意见已提交，来自 {sender_id} ({source})")
        except Exception as e:
            logger.error(f"[FeedbackPlugin] 提交 Teable 失败: {e}")
            yield event.plain_result("⚠️ 意见提交失败，请稍后再试。")
            return

        notify_msg = (
            f"📬 【新意见反馈】\n"
            f"🕐 {now}\n"
            f"👤 {sender_name}（{sender_id}）\n"
            f"📍 {source}\n"
            f"💬 {content}\n\n"
            f"发送「拉取意见」可查看所有待处理意见。"
        )
        platform_name = event.platform_meta.name
        for admin_qq in self.admin_ids:
            await _send_private(self.context, platform_name, admin_qq, notify_msg)

        yield event.plain_result("✅ 感谢你的反馈！你的意见已成功提交。")

    # ── /拉取意见 ───────────────────────────────────────────────────────────────
    @filter.command("拉取意见")
    async def pull_feedback(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            return

        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.get(
                    TEABLE_API,
                    headers={"Authorization": f"Bearer {TEABLE_TOKEN}"},
                    params={
                        "filter": '{"conjunction":"and","filterSet":[{"fieldId":"状态","operator":"is","value":"待处理"}]}',
                        "fieldKeyType": "name",
                    },
                )
                data = await resp.json()
        except Exception as e:
            yield event.plain_result(f"❌ 拉取失败: {e}")
            return

        records = data.get("records", [])
        if not records:
            yield event.plain_result("✅ 当前没有待处理的意见。")
            return

        mapping: dict[int, dict] = {}
        lines = [f"📋 待处理意见（共 {len(records)} 条）\n"]
        for i, record in enumerate(records, 1):
            f = record.get("fields", {})
            mapping[i] = {
                "record_id": record["id"],
                "submitter_qq": f.get("用户ID", ""),
                "submitter_name": f.get("用户昵称", "未知"),
                "title": f.get("意见内容", "")[:30],
                "source": f.get("来源", ""),
            }
            lines.append(
                f"[{i}] 👤{f.get('用户昵称', '未知')}（{f.get('用户ID', '')}）\n"
                f"    📍 {f.get('来源', '')}\n"
                f"    💬 {f.get('意见内容', '')}\n"
                f"    🕐 {f.get('时间', '')}"
            )

        _save_mapping(mapping)

        lines.append(
            "\n──────────────────\n"
            "指令说明：\n"
            "驳回 1        → 驳回第1条\n"
            "驳回 1,3,5    → 批量驳回\n"
            "驳回 2 原因：重复  → 带原因驳回\n"
            "批了 1        → 标记已通过\n"
            "批了 1,2,3    → 批量通过\n"
            "上线 1        → 标记第1条功能已落地并通知用户\n"
            "上线 1,2,3    → 批量标记落地"
        )

        yield event.plain_result("\n".join(lines))

    # ── /拉取已通过 ─────────────────────────────────────────────────────────────
    @filter.command("拉取已通过")
    async def pull_approved(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            return

        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.get(
                    TEABLE_API,
                    headers={"Authorization": f"Bearer {TEABLE_TOKEN}"},
                    params={
                        "filter": '{"conjunction":"and","filterSet":[{"fieldId":"fldi6VUiw2kwi1TnJHe","operator":"isNotEmpty","isSymbol":false,"value":null}]}',
                        "fieldKeyType": "name",
                    },
                )
                data = await resp.json()
        except Exception as e:
            yield event.plain_result(f"❌ 拉取失败: {e}")
            return

        records = data.get("records", [])
        records = [r for r in records if r.get("fields", {}).get("预计落地时间")]
        if not records:
            yield event.plain_result("✅ 当前没有已规划落地时间的功能。")
            return

        mapping: dict[int, dict] = {}
        lines = [f"📋 待落地功能（共 {len(records)} 条）\n"]
        for i, record in enumerate(records, 1):
            f = record.get("fields", {})
            mapping[i] = {
                "record_id": record["id"],
                "submitter_qq": f.get("用户ID", ""),
                "submitter_name": f.get("用户昵称", "未知"),
                "title": f.get("意见内容", "")[:30],
                "source": f.get("来源", ""),
            }
            landing_time = f.get("预计落地时间", "")[:10]
            lines.append(f"[{i}] {landing_time}  {f.get('意见内容', '')}")

        _save_mapping(mapping)
        lines.append("\n──────────────────\n上线 1 → 标记落地并通知用户\n征集 1 → 去来源群发起意见征集")
        yield event.plain_result("\n".join(lines))


    @filter.command("驳回")
    async def reject_feedback(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            return

        raw = event.message_str.strip()
        for prefix in ["/驳回", "驳回"]:
            if raw.startswith(prefix):
                raw = raw[len(prefix):].strip()
                break

        reason = ""
        if "原因：" in raw:
            raw, reason = raw.split("原因：", 1)
            reason = reason.strip()
        elif "原因:" in raw:
            raw, reason = raw.split("原因:", 1)
            reason = reason.strip()

        indices = self._parse_indices(raw)
        if not indices:
            yield event.plain_result("❌ 格式错误，例如：驳回 1,3 原因：重复提交")
            return

        mapping = _load_mapping()
        platform_name = event.platform_meta.name
        results = []

        for idx in indices:
            item = mapping.get(idx)
            if not item:
                results.append(f"[{idx}] ❌ 序号不存在，请重新「拉取意见」")
                continue
            try:
                fields = {"状态": "已驳回", "驳回": True}
                if reason:
                    fields["驳回原因"] = reason
                await _update_record(item["record_id"], fields)
                notify = (
                    f"你提交的意见「{item['title']}」已被驳回。"
                    + (f"\n原因：{reason}" if reason else "")
                )
                await _notify_submitter(self.context, platform_name, item, notify)
                results.append(f"[{idx}] ✅ 已驳回 {item['submitter_name']}「{item['title']}」")
            except Exception as e:
                results.append(f"[{idx}] ❌ 操作失败: {e}")

        yield event.plain_result("\n".join(results))

    # ── /批了 ───────────────────────────────────────────────────────────────────
    @filter.command("批了")
    async def approve_feedback(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            return

        raw = event.message_str.strip()
        for prefix in ["/批了", "批了"]:
            if raw.startswith(prefix):
                raw = raw[len(prefix):].strip()
                break

        indices = self._parse_indices(raw)
        if not indices:
            yield event.plain_result("❌ 格式错误，例如：批了 1,3")
            return

        mapping = _load_mapping()
        platform_name = event.platform_meta.name
        results = []

        for idx in indices:
            item = mapping.get(idx)
            if not item:
                results.append(f"[{idx}] ❌ 序号不存在，请重新「拉取意见」")
                continue
            try:
                await _update_record(item["record_id"], {"状态": "已通过"})
                notify = f"你提交的意见「{item['title']}」已被采纳，感谢你的反馈！🎉"
                await _notify_submitter(self.context, platform_name, item, notify)
                results.append(f"[{idx}] ✅ 已通过 {item['submitter_name']}「{item['title']}」")
            except Exception as e:
                results.append(f"[{idx}] ❌ 操作失败: {e}")

        yield event.plain_result("\n".join(results))

    # ── /上线 ────────────────────────────────────────────────────────────────────
    @filter.command("上线")
    async def mark_online(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            return

        raw = event.message_str.strip()
        for prefix in ["/上线", "上线"]:
            if raw.startswith(prefix):
                raw = raw[len(prefix):].strip()
                break

        indices = self._parse_indices(raw)
        if not indices:
            yield event.plain_result("❌ 格式错误，例如：上线 1,3")
            return

        mapping = _load_mapping()
        platform_name = event.platform_meta.name
        results = []

        for idx in indices:
            item = mapping.get(idx)
            if not item:
                results.append(f"[{idx}] ❌ 序号不存在，请重新「拉取意见」")
                continue
            try:
                await _update_record(item["record_id"], {"落地": True})
                notify = f"🎉 你提交的意见「{item['title']}」已正式落地，感谢你的贡献！"
                await _notify_submitter(self.context, platform_name, item, notify)
                results.append(f"[{idx}] ✅ 已落地 {item['submitter_name']}「{item['title']}」")
            except Exception as e:
                results.append(f"[{idx}] ❌ 操作失败: {e}")

        yield event.plain_result("\n".join(results))

    # ── /功能预计落地 ────────────────────────────────────────────────────────────
    @filter.command("功能预计落地")
    async def feature_roadmap(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            return
        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.get(
                    TEABLE_API,
                    headers={"Authorization": f"Bearer {TEABLE_TOKEN}"},
                    params={
                        "filter": '{"conjunction":"and","filterSet":[{"fieldId":"fldi6VUiw2kwi1TnJHe","operator":"isNotEmpty","isSymbol":false,"value":null}]}',
                        "fieldKeyType": "name",
                    },
                )
                data = await resp.json()
        except Exception as e:
            yield event.plain_result(f"拉取失败: {e}")
            return

        records = data.get("records", [])
        if not records:
            yield event.plain_result("暂无已规划落地时间的功能。")
            return

        valid = []
        for record in records:
            f = record.get("fields", {})
            raw_time = f.get("预计落地时间", "")
            if raw_time:
                valid.append((raw_time, f.get("意见内容", "")))
        valid.sort(key=lambda x: x[0])

        lines = ["【功能落地计划】"]
        for raw_time, content_text in valid:
            landing_time = raw_time[:10]
            lines.append(f"{landing_time}  {content_text}")

        yield event.plain_result("\n".join(lines))

    # ── 工具：解析序号字符串 ────────────────────────────────────────────────────
    @staticmethod
    def _parse_indices(raw: str) -> list[int]:
        raw = raw.replace("，", ",").replace(" ", ",")
        indices = []
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit():
                indices.append(int(part))
        return indices

    # ── /征集 编号 ──────────────────────────────────────────────────────────────
    # 拉取意见后，对某条意见发起群内征集
    @filter.command("征集")
    async def start_collection(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            return

        raw = event.message_str.strip()
        for prefix in ["/征集", "征集"]:
            if raw.startswith(prefix):
                raw = raw[len(prefix):].strip()
                break

        indices = self._parse_indices(raw.split()[0] if raw else "")
        if len(indices) != 1:
            yield event.plain_result("❌ 格式：征集 编号 [补充描述]\n例如：征集 3 想了解大家希望怎么实现")
            return

        idx = indices[0]
        extra = raw[len(str(idx)):].strip()  # 编号后面的补充描述
        mapping = _load_mapping()
        item = mapping.get(idx)
        if not item:
            yield event.plain_result(f"❌ 序号 {idx} 不存在，请重新「拉取已通过」")
            return

        source = item.get("source", "")
        if not source.startswith("群 "):
            yield event.plain_result(f"❌ 该意见来源为「{source}」，无法发起群征集")
            return

        group_id = source.replace("群 ", "").strip()
        topic = item["title"]
        platform_name = event.platform_meta.name
        expire_time = time.time() + 24 * 3600

        _active_collections[group_id] = {
            "topic": topic,
            "record_id": item["record_id"],
            "expire_time": expire_time,
            "platform_name": platform_name,
        }

        notify = (
            f"📢 【功能意见征集】\n"
            f"💡 {topic}\n"
            + (f"📝 {extra}\n" if extra else "")
            + f"\n欢迎大家发表看法！\n"
            f"发送「建议 你的想法」参与征集，征集将在24小时后关闭。"
        )
        await _send_group(self.context, platform_name, group_id, notify)
        yield event.plain_result(f"✅ 已在群 {group_id} 发起征集：{topic}")

    # ── 监听群消息中的「建议 xxx」 ────────────────────────────────────────────────
    @filter.command("建议")
    async def collect_suggestion(self, event: AstrMessageEvent):
        # 只处理群消息
        try:
            gid = str(event.message_obj.group_id)
        except Exception:
            return

        collection = _active_collections.get(gid)
        if not collection:
            return

        # 检查是否过期
        if time.time() > collection["expire_time"]:
            del _active_collections[gid]
            return

        raw = event.message_str.strip()
        for prefix in ["/建议", "建议"]:
            if raw.startswith(prefix):
                raw = raw[len(prefix):].strip()
                break

        if not raw:
            return

        sender_name = event.get_sender_name() if hasattr(event, "get_sender_name") else "未知用户"
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        record_id = collection.get("record_id")

        # 先读取现有功能备注
        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.get(
                    f"{TEABLE_API}/{record_id}",
                    headers={"Authorization": f"Bearer {TEABLE_TOKEN}"},
                    params={"fieldKeyType": "id"},
                )
                data = await resp.json()
            existing = data.get("fields", {}).get("fldMkWexZG9fPlrhsO7", "") or ""
        except Exception:
            existing = ""

        new_note = f"{existing}\n[{now}] {sender_name}：{raw}".strip()

        try:
            await _update_record(record_id, {"fldMkWexZG9fPlrhsO7": new_note}, field_key_type="id")
            yield event.plain_result("✅ 你的建议已收到，感谢参与！")
        except Exception as e:
            logger.error(f"[FeedbackPlugin] 征集建议写入失败: {e}")
            yield event.plain_result("⚠️ 提交失败，请稍后再试。")