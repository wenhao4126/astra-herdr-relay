#!/usr/bin/env python3
"""
Codex 多代理工具的 PreToolUse 桥接。
转发 Astra 已决定的创建或消息请求至 Relay，并阻断原生内部工具，
避免创建隐藏子代理。
"""
import fcntl, hashlib, json, os, re, subprocess, sys, tempfile, uuid
from contextlib import contextmanager
from pathlib import Path

RELAY=os.path.expanduser("~/.local/bin/astra-relay")
STATE=Path.home()/".local/state/astra-herdr-relay"
MAP=STATE/"hook-agent-map.json"

def load_map():
    try: mapping=json.loads(MAP.read_text())
    except FileNotFoundError: return {"targets":{},"sessions":{}}
    if not isinstance(mapping,dict) or not isinstance(mapping.get("targets"),dict) or not isinstance(mapping.get("sessions",{}),dict):
        raise ValueError("Hook 映射结构非法")
    if any(not isinstance(v,str) for v in mapping["targets"].values()) or any(not isinstance(v,dict) or any(not isinstance(a,str) for a in v.values()) for v in mapping.get("sessions",{}).values()):
        raise ValueError("Hook 别名结构非法")
    return mapping

@contextmanager
def map_lock():
    MAP.parent.mkdir(parents=True,exist_ok=True)
    with MAP.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        try: yield
        finally: fcntl.flock(lock,fcntl.LOCK_UN)

def save_map(x):
    fd,tmp=tempfile.mkstemp(prefix=MAP.name+".",suffix=".tmp",dir=MAP.parent)
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as f: json.dump(x,f,ensure_ascii=False,indent=2)
        os.replace(tmp,MAP)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def remember(sid,target,agent):
    # 外部创建完成后再加锁，锁内重新读取，避免覆盖其他进程的记录。
    with map_lock():
        mapping=load_map()
        aliases=mapping.setdefault("sessions",{}).setdefault(sid,{}) if sid else mapping["targets"]
        aliases[target]=agent
        mapping["targets"][agent]=agent
        save_map(mapping)

def resolve(sid,target):
    with map_lock(): mapping=load_map()
    targets=mapping["targets"]; sessions=mapping.get("sessions",{})
    # 明确的 Relay 名称优先，旧的无会话别名仅供无会话调用使用。
    if target in targets.values() or any(target in aliases.values() for aliases in sessions.values()): return target
    aliases=sessions.get(sid,{}) if sid else targets
    if target in aliases: return aliases[target]
    if target in targets or any(target in aliases for aliases in sessions.values()):
        raise ValueError(f"当前会话没有别名：{target}")
    return target

def run(args,timeout=25):
    return subprocess.run(args,text=True,capture_output=True,timeout=timeout)
def deny(msg):
    print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny",
          "permissionDecisionReason":msg}},ensure_ascii=False))
def role_for(inp):
    # 仅按显式角色及任务名前缀确定角色。
    allowed={"worker","fixer","reviewer","tester","auditor"}
    at=str(inp.get("agent_type") or "").lower()
    if at in allowed: return at
    tn=str(inp.get("task_name") or "").lower()
    for r in allowed:
        if tn==r or tn.startswith(r+"_") or tn.startswith(r+"-"): return r
    return "worker"
def safe_name(inp,role,event=None):
    raw=str(inp.get("task_name") or "").lower().replace("_","-")
    raw=re.sub(r"[^a-z0-9-]+","-",raw).strip("-")
    if not raw or not raw[0].isalpha(): raw=f"{role}-{raw or 'agent'}"
    # 物理名称带上本次会话调用的安全片段，避免不同会话的同名任务冲突。
    # 身份字段属于顶层 Hook 事件，不能从工具参数中读取。
    identity_source=event if event is not None else inp
    identity="|".join(str(identity_source.get(key) or "") for key in ("session_id", "turn_id", "tool_use_id"))
    stable=hashlib.sha256(identity.encode()).hexdigest()[:6] if identity else "anon"
    unique=uuid.uuid4().hex[:6]
    return (f"codex-{raw[:13].rstrip('-')}-{stable}{unique}")[:32].rstrip("-")
def tmptext(text):
    f=tempfile.NamedTemporaryFile("w",encoding="utf-8",delete=False,prefix="astra-relay-hook-",suffix=".txt")
    f.write(text); f.close(); return f.name

try:
    ev=json.load(sys.stdin)
    tool=ev.get("tool_name","")
    inp=ev.get("tool_input") or {}
    cwd=ev.get("cwd") or os.getcwd()
    sid=str(ev.get("session_id") or "")
    tid=str(ev.get("turn_id") or "")
    parent=f"codex:{sid}:{tid}" if sid or tid else None

    if tool=="Agent":
        deny("RELAY_MANAGED_AGENT: tool_name=Agent is blocked; use a Relay-managed visible Herdr agent instead of a hidden native agent.")
        raise SystemExit(0)

    if tool=="spawn_agent":
        message=inp.get("message")
        if not isinstance(message,str) or not message:
            deny("RELAY_REDIRECT_FAILED: spawn_agent had no plain-text message; native spawn blocked to avoid a hidden agent.")
            raise SystemExit(0)
        role=role_for(inp); requested=safe_name(inp,role,ev)
        cmd=[RELAY,"spawn","--role",role,"--cwd",cwd,"--name",requested]
        if parent: cmd += ["--parent-task-id",parent]
        r=run(cmd,timeout=125)
        if r.returncode:
            # 创建可能已产生资源，失败后禁止自动重试。
            deny(f"RELAY_REDIRECT_FAILED: requested_agent={requested} "+(r.stderr or r.stdout)[-1200:]); raise SystemExit(0)
        rec=json.loads(r.stdout); agent=rec["name"]
        fp=tmptext(message)
        try:
            d=run([RELAY,"dispatch-opaque",agent,"--prompt-file",fp]+(["--parent-task-id",parent] if parent else []))
        finally:
            try: os.unlink(fp)
            except OSError: pass
        if d.returncode:
            deny(f"RELAY_REDIRECT_PARTIAL: Herdr agent {agent} was created but task dispatch failed: {(d.stderr or d.stdout)[-900:]}")
            raise SystemExit(0)
        dispatched=json.loads(d.stdout)
        # 即使旧版命令错误地返回零，也必须核实任务确实被接收。
        if dispatched.get("accepted") is not True:
            deny(f"RELAY_REDIRECT_PARTIAL: Herdr agent {agent} was created but task dispatch was not accepted: {str(dispatched.get('error', 'missing acceptance'))[-900:]}")
            raise SystemExit(0)
        task=dispatched.get("task_id")
        if not isinstance(task,str) or not re.fullmatch(r"relay-[a-zA-Z0-9_-]+",task):
            deny(f"RELAY_REDIRECT_PARTIAL: Herdr agent {agent} was created and dispatch was accepted, but task_id is invalid: {task!r}")
            raise SystemExit(0)
        # 保存当前会话的任务别名及明确的 Relay 名称。
        target=str(inp.get("task_name") or agent)
        try:
            remember(sid,target,agent)
        except Exception as exc:
            deny(f"RELAY_REDIRECT_PARTIAL: Herdr agent {agent} was created and task {task} was accepted, but mapping save failed: {str(exc)[-900:]}")
            raise SystemExit(0)
        deny(f"RELAY_REDIRECT_OK: native internal spawn was blocked. Astra's original message was forwarded unchanged to visible Herdr agent '{agent}' via Relay. relay_task_id={task}. Use Relay-managed messaging/status/release for this agent.")
        raise SystemExit(0)

    if tool in {"send_message","followup_task"}:
        target=str(inp.get("target") or "")
        agent=resolve(sid,target)
        message=inp.get("message")
        if not target or not isinstance(message,str):
            deny("RELAY_REDIRECT_FAILED: missing target/message."); raise SystemExit(0)
        fp=tmptext(message)
        try: r=run([RELAY,"send-opaque",agent,"--message-file",fp])
        finally:
            try: os.unlink(fp)
            except OSError: pass
        deny(("RELAY_REDIRECT_OK: message forwarded unchanged to "+agent) if r.returncode==0
             else "RELAY_REDIRECT_FAILED: "+(r.stderr or r.stdout)[-1000:])
        raise SystemExit(0)

    if tool=="close_agent":
        target=str(inp.get("target") or inp.get("id") or "")
        agent=resolve(sid,target)
        r=run([RELAY,"release",agent])
        deny(("RELAY_REDIRECT_OK: Relay released "+agent) if r.returncode==0
             else "RELAY_REDIRECT_FAILED: "+(r.stderr or r.stdout)[-1000:])
        raise SystemExit(0)

    if tool in {"wait_agent","interrupt_agent","resume_agent","list_agents"}:
        deny("RELAY_MANAGED_AGENT: native internal agent-control tool blocked. Query/control the Relay-managed Herdr agent through astra-relay instead.")
        raise SystemExit(0)

    # 允许不属于匹配范围的调用。
    print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}))
except Exception as e:
    # 异常时阻断原生创建，报告请求名称供人工核实。
    deny(f"RELAY_HOOK_ERROR: requested_agent={locals().get('requested', '未创建')} "+str(e)[:1000])
