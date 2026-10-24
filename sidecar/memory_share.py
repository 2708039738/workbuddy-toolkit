#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨账号全局记忆汇总（共享记忆库）。

与"覆盖式同步"的根本区别：

1. **汇总**：把所有账号 MEMORY.md 的条目收进一个库，而不是拿 A 的副本盖掉 B。
2. **去重择优**：相似条目聚成一簇，只让质量最高的一条参与注入；
   落选版本连同来源一起留档（superseded），不删除。
3. **不覆盖不丢失**：每个账号首次接入时把 MEMORY.md 存成基线快照；
   之后渲染 = 基线原文（一字不改）+ 共享段落（追加在后面）。
   账号自己新增的条目由 absorb() 增量并入基线，旧条目不会被顶掉。
4. **可还原**：restore() 把基线原样写回，写回前先备份当前文件。

只处理 MEMORY.md 一个文件。SOUL / IDENTITY / AGENT 属于"这个账号是谁"，
不参与汇总，避免人格互相污染。
"""

import difflib
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
from datetime import datetime

MEMORY_FILE = "MEMORY.md"

# 客户端 getUserLocalMemoryContent() 只注入前 4000 字，超出部分不会进上下文
INJECT_LIMIT = 4000

# 相似判定阈值：0.90 只合并"措辞几乎相同"的近重复。
# 刻意不做语义推断——把"用中文"和"不要用中文回复"判成同一条，比漏合并危险得多。
SIMILARITY_THRESHOLD = 0.90

# 每个条目最多保留几个落选版本（防止库无限膨胀）
MAX_SUPERSEDED = 5

BACKUP_KEEP = 10

BEGIN_MARK = "<!-- shared-memory:begin -->"
END_MARK = "<!-- shared-memory:end -->"
SHARED_HEADER = "## 共享记忆（来自其他账号）"
LIBRARY_VERSION = 1


# ----------------------------------------------------------------------------
# 路径
# ----------------------------------------------------------------------------
def default_root():
    return os.path.expanduser("~/.workbuddy")


def hub_dir(root=None):
    return os.path.normpath(os.path.join(root or default_root(), "cache", "shared_memory"))


def library_path(root=None):
    return os.path.join(hub_dir(root), "library.json")


def snapshot_path(root, uid):
    return os.path.join(hub_dir(root), "originals", _safe(uid), MEMORY_FILE)


def memory_dir(root, uid, enterprise_id=None):
    """复刻客户端的账号目录规则：user-<uid>-<enterpriseId|personal>。"""
    uid = str(uid or "").strip()
    if not uid:
        return None
    ent = str(enterprise_id or "").strip() or "personal"
    return os.path.normpath(os.path.join(root or default_root(), "user-{}-{}".format(uid, ent)))


def memory_file(root, uid, enterprise_id=None):
    d = memory_dir(root, uid, enterprise_id)
    return os.path.join(d, MEMORY_FILE) if d else None


def _safe(name):
    return re.sub(r"[^\w\-]", "_", str(name or "unknown"))[:64] or "unknown"


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ----------------------------------------------------------------------------
# 文件读写
# ----------------------------------------------------------------------------
def read_text(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return ""


def read_text_checked(path):
    """返回 (文本, 状态)。状态: ok | missing | not_utf8 | unreadable。

    非 UTF-8 必须与"空文件"区分开：把 GBK 文件当成空文件，会让基线快照被写成空、
    随后整篇被共享内容替换——这是不可接受的数据丢失。
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return "", "missing"
    except OSError:
        return "", "unreadable"
    try:
        return raw.decode("utf-8"), "ok"
    except UnicodeDecodeError:
        return "", "not_utf8"


def atomic_write_text(path, text):
    """同目录临时文件 + fsync + os.replace。客户端读记忆不做容错，不能出现半截文件。"""
    target_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-mem-", dir=target_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if sys.platform != "win32":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def backup_file(path, root):
    """覆盖前留一份带时间戳的副本，保留最近 BACKUP_KEEP 份。"""
    if not os.path.isfile(path):
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.join(hub_dir(root), "backups", _safe(os.path.basename(os.path.dirname(path))))
    os.makedirs(base, exist_ok=True)
    dest = os.path.join(base, "{}.md".format(stamp))
    seq = 1
    while os.path.exists(dest):
        dest = os.path.join(base, "{}-{}.md".format(stamp, seq))
        seq += 1
    shutil.copy2(path, dest)
    _prune(base)
    return dest


def _prune(base, keep=BACKUP_KEEP):
    try:
        names = sorted(n for n in os.listdir(base) if n.endswith(".md"))
    except OSError:
        return
    for name in names[:-keep] if len(names) > keep else []:
        try:
            os.remove(os.path.join(base, name))
        except OSError:
            pass


# ----------------------------------------------------------------------------
# 文本处理
# ----------------------------------------------------------------------------
def _managed_block_span(text):
    """返回受管段落的 (起, 止) 行号；结构不完整则返回 None。

    只认我们自己写出来的形状：起始标记独占一行 → 下一个非空行是标题 → 终止标记独占一行。
    这样用户正文里偶尔出现 `<!-- shared-memory:begin -->` 这几个字时不会被误判成
    "共享段开始"从而把后半篇截掉。
    """
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.strip() != BEGIN_MARK:
            continue
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j >= len(lines) or lines[j].strip() != SHARED_HEADER:
            continue
        for k in range(j, len(lines)):
            if lines[k].strip() == END_MARK:
                return i, k
        return None
    return None


def strip_shared_block(text):
    """去掉之前注入的共享段落，只留账号自己的原文。"""
    if not text:
        return text
    span = _managed_block_span(text)
    if span is None:
        return text
    start, end = span
    lines = text.splitlines(keepends=True)
    return ("".join(lines[:start]) + "".join(lines[end + 1:])).rstrip() + "\n"


def has_shared_block(text):
    return bool(text) and _managed_block_span(text) is not None


def normalize(text):
    """归一化：全角转半角、小写、去列表符号与标点空白。

    只用于比较，不用于回写——原文永远按原样保存。
    """
    t = unicodedata.normalize("NFKC", str(text or "")).lower()
    t = re.sub(r"^[\s\-*+>]+", "", t)
    t = re.sub(r"^\d+[.)]\s*", "", t)
    t = re.sub(r"[\s\W_]+", "", t, flags=re.UNICODE)
    return t


def _digits(norm):
    return tuple(re.findall(r"\d+", norm))


def similarity(a, b):
    """0~1。先做长度比剪枝，再算序列相似度，避免长文本两两比对的开销。

    两条硬规则（都是踩过坑补的）：
    - 数字序列不同 → 直接判不相似。"超时设为 30s" 和 "60s" 是两个事实，不是同一条
      记忆的两种写法；否则模板化的清单（只差编号）会被整片误合并。
    - 长度比 < 0.5 → 不相似。短句被长句包含的场景太容易误判。
    """
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if _digits(a) != _digits(b):
        return 0.0
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) / float(len(long_)) < 0.5:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


STRONG_WORDS = ("必须", "禁止", "不要", "默认", "一律", "始终", "永远", "只能",
                "must", "never", "always", "required")
HEDGE_WORDS = ("可能", "也许", "大概", "或许", "若干", "之类", "差不多", "应该是",
               "好像是", "大概是", "看情况")


def quality(text):
    """给一条记忆打 0~10 的质量分，返回 (分数, 理由列表)。

    刻意做成纯规则、可解释、可复现：同类实现里最容易出问题的是"用模型打分"——
    同样的输入两次分数不同，用户就无法理解为什么这条被保留、那条被丢掉。
    """
    raw = str(text or "").strip()
    norm = normalize(raw)
    if not norm:
        return 0.0, ["空内容"]
    score = 5.0
    reasons = []
    length = len(raw)

    if length < 6:
        score -= 2.0
        reasons.append("过短")
    elif length < 12:
        score -= 0.6
        reasons.append("偏短")
    elif length <= 120:
        score += 0.6
        reasons.append("长度适中")
    elif length > 240:
        score -= 0.8
        reasons.append("过长")

    if re.search(r"\d", raw):
        score += 0.6
        reasons.append("含具体数值")
    if re.search(r"[`/\\]|\b\w+\.(py|js|json|md|sh|ya?ml)\b", raw):
        score += 0.6
        reasons.append("含路径或代码")
    if any(w in raw.lower() for w in STRONG_WORDS):
        score += 0.8
        reasons.append("含明确约束")
    if re.search(r"^[\s]*[-*+]?\s*[^：:\n]{1,12}[：:]", raw):
        score += 0.5
        reasons.append("键值结构")

    hedges = sum(1 for w in HEDGE_WORDS if w in raw)
    if hedges:
        score -= min(1.2 * hedges, 2.4)
        reasons.append("含模糊措辞×{}".format(hedges))

    uniq_ratio = len(set(norm)) / float(len(norm))
    if uniq_ratio < 0.5:
        score -= 0.8
        reasons.append("重复度高")

    return round(max(0.0, min(10.0, score)), 2), reasons


def candidate_lines(text):
    """从记忆正文里挑出"条目行"：非空、不是标题、不是注释。"""
    out = []
    for line in str(text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("<!--"):
            continue
        if normalize(s):
            out.append(s)
    return out


def absorb(baseline_text, current_text):
    """把账号当前文件里的新条目并进基线，返回 (新基线, 新增数, 升级数)。

    规则：
    - 与基线里已有条目完全相同 → 跳过；
    - 相似但质量更高 → 用新的替换旧的那一行（旧的进库的 superseded）；
    - 都不相似 → 追加到末尾。
    基线里原有但当前文件没有的条目**不删除**——"不丢失"优先于"可删除"。
    """
    baseline = (baseline_text or "").rstrip()
    lines = baseline.splitlines()
    index = {normalize(l): i for i, l in enumerate(lines) if normalize(l)}
    added = upgraded = 0

    for line in candidate_lines(current_text):
        norm = normalize(line)
        if norm in index:
            continue
        best_i, best_sim = None, 0.0
        for existing_norm, i in index.items():
            sim = similarity(norm, existing_norm)
            if sim > best_sim:
                best_i, best_sim = i, sim
        if best_i is not None and best_sim >= SIMILARITY_THRESHOLD:
            old_q, _ = quality(lines[best_i])
            new_q, _ = quality(line)
            if new_q > old_q:
                del index[normalize(lines[best_i])]
                lines[best_i] = line
                index[norm] = best_i
                upgraded += 1
            continue
        lines.append(line)
        index[norm] = len(lines) - 1
        added += 1

    return "\n".join(lines).rstrip() + "\n", added, upgraded


def render(origin_text, shared_entries, limit=INJECT_LIMIT):
    """基线原文 + 共享段落。返回 (全文, 实际写入条数, 被截断条数)。

    字符预算优先给账号自己的内容：共享段落只能在剩余空间里塞，
    绝不为了塞共享而截断原文——那等于用别人的记忆挤掉自己的记忆。
    """
    origin = (origin_text or "").rstrip()
    if not shared_entries:
        return origin + "\n", 0, 0

    header = [BEGIN_MARK, SHARED_HEADER, ""]
    used = len(origin) + sum(len(x) + 1 for x in header) + len(END_MARK) + 2
    body = []
    skipped = 0
    for entry in shared_entries:
        line = "- " + entry["text"].strip()
        if used + len(line) + 1 > limit:
            skipped = 1
            break
        body.append(line)
        used += len(line) + 1

    if skipped:
        remaining = len(shared_entries) - len(body)
        footer = "- （另有 {} 条共享记忆未展开，超出单次注入上限）".format(remaining)
        # 结尾提示也要算进预算：不为了写这句话而突破 4000 字上限
        while body and used + len(footer) + 1 > limit:
            dropped = body.pop()
            used -= len(dropped) + 1
            remaining += 1
            footer = "- （另有 {} 条共享记忆未展开，超出单次注入上限）".format(remaining)
        if used + len(footer) + 1 <= limit:
            body.append(footer)

    if not body:
        return origin + "\n", 0, len(shared_entries)
    text = origin + "\n\n" + "\n".join(header + body + ["", END_MARK]) + "\n"
    return text, len(body), max(0, len(shared_entries) - len(body))


# ----------------------------------------------------------------------------
# 记忆分诊（rebalance）：把 MEMORY.md 的内容按性质分流到容量更大的常驻通道
# ----------------------------------------------------------------------------
# 客户端有四个常驻通道，上限不同：
#   MEMORY.md  4000   —— 事实、配置、项目状态
#   SOUL.md   10000   —— 行为准则、红线、流程
#   USER.md   10000   —— 称呼、语言、沟通偏好
#   IDENTITY.md 10000 —— 身份元数据（不放记忆）
# 把全部内容堆在 MEMORY.md 是容量浪费：分诊后同样一份记忆可以完整保留，
# 而不是被 4000 字截掉三分之一。

REBALANCE_BEGIN = "<!-- memory-rebalance:begin -->"
REBALANCE_END = "<!-- memory-rebalance:end -->"

USER_MARKERS = ("称呼", "叫我", "昵称", "名字", "语言", "中文", "英文",
                "沟通", "语气", "画像", "回复风格", "称呼为")
SOUL_MARKERS = ("必须", "禁止", "不要", "不得", "默认", "一律", "始终", "永远",
                "优先", "流程", "步骤", "红线", "纪律", "规则", "规范",
                "提交", "测试", "分支", "推送", "审查", "命名", "格式")


def classify_line(line):
    """按"关于人 / 怎么做 / 什么事"给一行记忆分诊。

    纯关键词规则，刻意保守：分不清就留在 MEMORY（原处），不硬塞。
    迁移是可逆的，但把事实误塞进人格通道会让行为变怪，代价比多占几百字高。
    """
    s = str(line or "").strip()
    if not s or s.startswith("#") or s.startswith("<!--"):
        return "memory"
    if any(k in s for k in USER_MARKERS):
        return "user"
    if any(k in s for k in SOUL_MARKERS):
        return "soul"
    return "memory"


def strip_rebalance_section(text):
    """移除上一次分诊写入的受管段落（SOUL/USER 各一段）。"""
    if not text or REBALANCE_BEGIN not in text:
        return text
    start = text.find(REBALANCE_BEGIN)
    end = text.find(REBALANCE_END)
    if end == -1:
        return text
    return (text[:start] + text[end + len(REBALANCE_END):]).rstrip() + "\n"


def plan_rebalance(memory_text, target=3200):
    """算出"哪些行留在 MEMORY、哪些搬到 SOUL/USER"。

    只在 MEMORY 超过 target 时才搬，且优先搬 SOUL 类（通常占比最大），
    搬到 target 以下立即停止——不做无谓的整体迁移。
    """
    lines = str(memory_text or "").splitlines()
    kinds = [classify_line(line) for line in lines]
    total = sum(len(line) + 1 for line in lines)
    moved_idx = set()
    moved = {"soul": [], "user": []}

    remaining = total
    if remaining > target:
        for kind in ("soul", "user"):
            for i, line in enumerate(lines):
                if remaining <= target:
                    break
                if kinds[i] != kind or i in moved_idx:
                    continue
                moved[kind].append(line)
                moved_idx.add(i)
                remaining -= len(line) + 1

    kept = [line for i, line in enumerate(lines) if i not in moved_idx]
    kept_size = sum(len(line) + 1 for line in kept)
    return {
        "lines_in": len(lines),
        "memory_lines": len(kept),
        "memory_size": kept_size,
        "target": target,
        "moved_soul": moved["soul"],
        "moved_user": moved["user"],
        "moved_soul_size": sum(len(x) + 1 for x in moved["soul"]),
        "moved_user_size": sum(len(x) + 1 for x in moved["user"]),
        "kept": kept,
        "overflow_before": max(0, total - target),
    }


def _render_channel_section(title, lines):
    body = "\n".join(lines)
    return "{}\n## {}\n\n{}\n{}\n".format(REBALANCE_BEGIN, title, body, REBALANCE_END)


def rebalance_account(acc, root=None, target=3200, apply=False):
    """把一个账号的记忆按通道重新分配。

    apply=False（默认）只返回计划，不写任何文件。
    写入顺序：先备份三个文件 → 写 SOUL/USER 的新段落 → 最后改 MEMORY，
    这样即使中途失败，MEMORY 原文仍在，最坏情况是多了两段可清理的内容。
    """
    root = root or default_root()
    uid = str(acc.get("uid") or "").strip()
    d = memory_dir(root, uid, acc.get("enterprise_id"))
    result = {"ok": False, "alias": acc.get("alias") or uid, "apply": bool(apply),
              "plan": None, "written": [], "backups": []}
    if not d:
        result["action"] = "no_memory_file"
        return result
    mem_path = os.path.join(d, MEMORY_FILE)
    soul_path = os.path.join(d, "SOUL.md")
    user_path = os.path.join(d, "USER.md")
    mem_text, state = read_text_checked(mem_path)
    if state != "ok":
        result["action"] = "skipped_encoding" if state == "not_utf8" else "no_memory_file"
        return result

    plan = plan_rebalance(mem_text, target=target)
    soul_before = len(read_text(soul_path))
    user_before = len(read_text(user_path))
    result["projected"] = {
        "memory_before": len(mem_text),
        "memory_after": plan["memory_size"],
        "soul_before": soul_before,
        "soul_after": soul_before + plan["moved_soul_size"],
        "user_before": user_before,
        "user_after": user_before + plan["moved_user_size"],
    }
    result["plan"] = {
        "memory_size": plan["memory_size"],
        "moved_soul": len(plan["moved_soul"]),
        "moved_user": len(plan["moved_user"]),
        "moved_soul_size": plan["moved_soul_size"],
        "moved_user_size": plan["moved_user_size"],
        "target": target,
    }
    if not apply or (not plan["moved_soul"] and not plan["moved_user"]):
        result["ok"] = True
        result["action"] = "planned" if apply is False else "nothing_to_do"
        return result

    # 先留存基线快照（供 restore 还原），再备份三个文件，最后才写
    _ensure_snapshot(root, uid, mem_path, current_text=mem_text)
    for p in (mem_path, soul_path, user_path):
        b = backup_file(p, root)
        if b:
            result["backups"].append(b)

    if plan["moved_soul"]:
        soul_text = strip_rebalance_section(read_text(soul_path))
        soul_text = soul_text.rstrip() + "\n\n" + _render_channel_section(
            "工作准则（来自长期记忆）", plan["moved_soul"])
        atomic_write_text(soul_path, soul_text)
        result["written"].append("SOUL.md")

    if plan["moved_user"]:
        user_text = strip_rebalance_section(read_text(user_path))
        user_text = user_text.rstrip() + "\n\n" + _render_channel_section(
            "用户偏好（来自长期记忆）", plan["moved_user"])
        atomic_write_text(user_path, user_text)
        result["written"].append("USER.md")

    atomic_write_text(mem_path, "\n".join(plan["kept"]).rstrip() + "\n")
    result["written"].append(MEMORY_FILE)
    result["ok"] = True
    result["action"] = "rebalanced"
    return result
# ----------------------------------------------------------------------------
# 共享库
# ----------------------------------------------------------------------------
def empty_library():
    return {"version": LIBRARY_VERSION, "updated_at": None, "entries": []}


def load_library(root=None):
    try:
        with open(library_path(root), "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return empty_library()
    if not isinstance(raw, dict):
        return empty_library()
    entries = raw.get("entries")
    lib = empty_library()
    lib["updated_at"] = raw.get("updated_at")
    lib["entries"] = entries if isinstance(entries, list) else []
    return lib


def save_library(lib, root=None):
    lib["updated_at"] = now_iso()
    atomic_write_text(library_path(root), json.dumps(lib, ensure_ascii=False, indent=2) + "\n")


def _entry_id(norm):
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


def _cluster(items):
    """并查集聚类：相似度 ≥ 阈值的条目归为一簇。"""
    n = len(items)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            if similarity(items[i]["norm"], items[j]["norm"]) >= SIMILARITY_THRESHOLD:
                union(i, j)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def collect(accounts, root=None):
    """扫描所有账号的 MEMORY.md，汇总去重后写库。

    accounts: [{"alias","uid","enterprise_id"}]
    返回 {ok, accounts, raw_entries, entries, merged, superseded}
    """
    root = root or default_root()
    old = load_library(root)
    old_by_id = {e.get("id"): e for e in old.get("entries") or []}

    items = []
    per_account = {}
    skipped = []
    for acc in accounts or []:
        uid = str(acc.get("uid") or "").strip()
        path = memory_file(root, uid, acc.get("enterprise_id"))
        if not path:
            continue
        raw, state = read_text_checked(path)
        alias = acc.get("alias") or uid
        if state == "not_utf8":
            per_account[alias] = "跳过（非 UTF-8）"
            skipped.append(alias)
            continue
        text = strip_shared_block(raw)
        lines = candidate_lines(text)
        per_account[alias] = len(lines)
        for line in lines:
            norm = normalize(line)
            score, _ = quality(line)
            items.append({
                "text": line.strip(),
                "norm": norm,
                "uid": uid,
                "alias": acc.get("alias") or "",
                "quality": score,
            })

    merged = 0
    superseded_count = 0
    entries = []
    for group in _cluster(items) if items else []:
        versions = [items[i] for i in group]
        versions.sort(key=lambda v: (v["quality"], len(v["norm"])), reverse=True)
        winner = versions[0]
        eid = _entry_id(winner["norm"])
        prev = old_by_id.get(eid) or {}
        losers = []
        for loser in versions[1:]:
            if normalize(loser["text"]) == normalize(winner["text"]):
                continue
            losers.append({
                "text": loser["text"],
                "uid": loser["uid"],
                "quality": loser["quality"],
            })
        if versions[1:]:
            merged += 1
        superseded_count += len(losers)
        entries.append({
            "id": eid,
            "text": winner["text"],
            "norm": winner["norm"],
            "quality": winner["quality"],
            "sources": sorted({v["uid"] for v in versions if v["uid"]}),
            "aliases": sorted({v["alias"] for v in versions if v["alias"]}),
            "superseded": (prev.get("superseded") or []) + losers,
            "first_seen": prev.get("first_seen") or now_iso(),
            "last_seen": now_iso(),
        })

    for e in entries:
        if len(e["superseded"]) > MAX_SUPERSEDED:
            e["superseded"] = e["superseded"][:MAX_SUPERSEDED]

    entries.sort(key=lambda e: (-e["quality"], e["text"]))
    lib = {"version": LIBRARY_VERSION, "updated_at": now_iso(), "entries": entries}
    save_library(lib, root)
    return {
        "ok": True,
        "accounts": per_account,
        "skipped": skipped,
        "raw_entries": len(items),
        "entries": len(entries),
        "merged": merged,
        "superseded": superseded_count,
    }


def _ensure_snapshot(root, uid, memory_path, current_text=None):
    """首次接入时保存基线快照；已存在则原样返回，绝不覆盖。"""
    path = snapshot_path(root, uid)
    if os.path.isfile(path):
        return read_text(path)
    current = strip_shared_block(current_text if current_text is not None else read_text(memory_path))
    atomic_write_text(path, current)
    return current


def apply_account(acc, root=None, lib=None):
    """把一个账号的 MEMORY.md 渲染成「基线原文 + 共享段落」。

    返回 {ok, alias, action, shared, truncated, added, upgraded, backup}
    action: created | updated | unchanged | no_memory_file | error
    """
    root = root or default_root()
    lib = lib or load_library(root)
    uid = str(acc.get("uid") or "").strip()
    alias = acc.get("alias") or uid
    path = memory_file(root, uid, acc.get("enterprise_id"))
    result = {"ok": False, "alias": alias, "action": "error", "shared": 0,
              "truncated": 0, "added": 0, "upgraded": 0, "backup": None}
    if not path:
        result["action"] = "no_memory_file"
        return result
    if not os.path.isfile(path):
        result["action"] = "no_memory_file"
        return result

    current, state = read_text_checked(path)
    if state == "not_utf8":
        # 读不了就不动：宁可这次不共享，也不能把 GBK/UTF-16 文件当空文件覆盖掉
        result["ok"] = True
        result["action"] = "skipped_encoding"
        result["reason"] = "MEMORY.md 不是 UTF-8 编码，已跳过（未做任何写入）"
        return result
    baseline = _ensure_snapshot(root, uid, path, current_text=current)
    new_baseline, added, upgraded = absorb(baseline, strip_shared_block(current))
    if new_baseline != baseline:
        backup_file(snapshot_path(root, uid), root)
        atomic_write_text(snapshot_path(root, uid), new_baseline)
    result["added"] = added
    result["upgraded"] = upgraded

    own_norms = [normalize(l) for l in candidate_lines(new_baseline)]
    shared = []
    for entry in sorted(lib.get("entries") or [], key=lambda e: -e.get("quality", 0)):
        norm = entry.get("norm") or normalize(entry.get("text", ""))
        if not norm:
            continue
        if any(norm == n for n in own_norms):
            continue
        if any(similarity(norm, n) >= SIMILARITY_THRESHOLD for n in own_norms):
            continue
        shared.append(entry)

    rendered, written, truncated = render(new_baseline, shared)
    result["shared"] = written
    result["truncated"] = truncated

    if rendered == current:
        result["ok"] = True
        result["action"] = "unchanged"
        return result

    result["backup"] = backup_file(path, root)
    try:
        atomic_write_text(path, rendered)
    except OSError as e:
        result["ok"] = False
        result["action"] = "write_failed"
        result["reason"] = str(e)
        return result
    result["ok"] = True
    result["action"] = "created" if not has_shared_block(current) else "updated"
    return result


def apply_all(accounts, root=None):
    lib = load_library(root)
    return [apply_account(a, root=root, lib=lib) for a in accounts or []]


def restore_account(acc, root=None):
    """把基线快照原样写回账号文件；写回前备份当前文件。"""
    root = root or default_root()
    uid = str(acc.get("uid") or "").strip()
    path = memory_file(root, uid, acc.get("enterprise_id"))
    snap = snapshot_path(root, uid)
    result = {"ok": False, "action": "error", "backup": None, "alias": acc.get("alias") or uid}
    if not os.path.isfile(snap):
        result["action"] = "no_snapshot"
        return result
    if not path:
        result["action"] = "no_memory_file"
        return result
    result["backup"] = backup_file(path, root)
    atomic_write_text(path, read_text(snap))
    result["ok"] = True
    result["action"] = "restored"
    return result


def status(accounts, root=None):
    root = root or default_root()
    lib = load_library(root)
    rows = []
    for acc in accounts or []:
        uid = str(acc.get("uid") or "").strip()
        path = memory_file(root, uid, acc.get("enterprise_id"))
        current = read_text(path) if path else ""
        own = strip_shared_block(current)
        shared_lines = 0
        if has_shared_block(current):
            block = current[current.find(BEGIN_MARK):current.find(END_MARK)]
            shared_lines = len([l for l in block.splitlines() if l.strip().startswith("- ")])
        rows.append({
            "alias": acc.get("alias") or uid,
            "uid": uid,
            "file": path,
            "exists": bool(path and os.path.isfile(path)),
            "snapshot": os.path.isfile(snapshot_path(root, uid)),
            "own_chars": len(own.strip()),
            "shared_entries": shared_lines,
            "total_chars": len(current.strip()),
            "over_limit": len(current.strip()) > INJECT_LIMIT,
        })
    return {"library": {"entries": len(lib.get("entries") or []),
                        "updated_at": lib.get("updated_at"),
                        "path": library_path(root)},
            "accounts": rows}


# ----------------------------------------------------------------------------
# 独立命令行（供人工核对与排障）
# ----------------------------------------------------------------------------
def _print_status(data):
    lib = data["library"]
    print("共享记忆库: {} 条  ({})".format(lib["entries"], lib["path"]))
    for row in data["accounts"]:
        if not row["exists"]:
            print("  {:<12} 无 MEMORY.md".format(row["alias"]))
            continue
        print("  {:<12} 自有 {:>5} 字 | 共享 {:>2} 条 | 合计 {:>5} 字{}".format(
            row["alias"], row["own_chars"], row["shared_entries"], row["total_chars"],
            "  超 4000 字上限" if row["over_limit"] else ""))


def main(argv=None, root=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    root = root or default_root()
    if not argv or argv[0] in ("status", "-h", "--help"):
        _print_status(status([], root=root))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
