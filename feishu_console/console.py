import os

from .feishu_client import demo_card

HELP = """可用命令：
  list                          列出已收到的消息
  show <序号>                   打印该消息的完整原始 JSON
  reply <序号> <文本>           回复文本
  send <open_id> <文本>         主动发文本（open_id 形如 ou_xxxx）
  card <序号>                   回复演示卡片（带按钮，点击触发回调）
  stream <序号> <文本>          流式回复·方案A（PATCH 整卡逐字更新）
  stream2 <序号> <文本>         流式回复·方案B（CardKit 按 sequence 增量更新）
  file <序号> <本地路径>        上传本地文件并作为文件消息回复
  download <序号> [保存目录]    下载收到的文件/图片（默认存到 downloads/）
  help                          显示本帮助
  quit                          退出程序"""


def run_console(store, feishu):
    print("\n===== 终端控制台已就绪 =====")
    print(HELP)
    while True:
        try:
            line = input("feishu> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue

        cmd, _, rest = line.partition(" ")
        cmd = cmd.lower()

        try:
            if cmd in ("quit", "exit"):
                print("再见。")
                os._exit(0)
            elif cmd == "help":
                print(HELP)
            elif cmd == "list":
                _cmd_list(store)
            elif cmd == "show":
                m = store.get(_to_int(rest))
                print(m["raw_json"] if m else f"消息 #{rest} 不存在")
            elif cmd == "reply":
                seq_str, _, text = rest.partition(" ")
                m = store.get(_to_int(seq_str))
                if not m:
                    print(f"消息 #{seq_str} 不存在，先用 list 查看序号")
                    continue
                if not text:
                    print("用法: reply <序号> <文本>")
                    continue
                print(f"回复 #{m['seq']}: {feishu.reply_text(m['message_id'], text)}")
            elif cmd == "send":
                open_id, _, text = rest.partition(" ")
                if not open_id or not text:
                    print("用法: send <open_id> <文本>")
                    continue
                print(f"发送结果: {feishu.send_text(open_id, text)}")
            elif cmd == "card":
                m = _get_msg(store, rest)
                if m:
                    print(f"回复卡片: {feishu.reply_card(m['message_id'], demo_card())}")
            elif cmd in ("stream", "stream2"):
                seq_str, _, text = rest.partition(" ")
                m = _get_msg(store, seq_str)
                if not m:
                    continue
                if not text:
                    print(f"用法: {cmd} <序号> <文本>")
                    continue
                if cmd == "stream":
                    print(f"开始流式回复 #{m['seq']}（PATCH 整卡）...")
                    print(f"完成: {feishu.stream_patch(m['message_id'], text)}")
                else:
                    print(f"开始流式回复 #{m['seq']}（SDK FeishuChannel 内置流式）...")
                    print(f"完成: {feishu.stream_cardkit(m['chat_id'], m['message_id'], text)}")
            elif cmd == "file":
                seq_str, _, path = rest.partition(" ")
                m = _get_msg(store, seq_str)
                if not m:
                    continue
                if not path or not os.path.isfile(path):
                    print(f"用法: file <序号> <本地路径>（文件不存在: {path}）")
                    continue
                print(f"上传并回复文件: {feishu.reply_local_file(m['message_id'], path)}")
            elif cmd == "download":
                seq_str, _, save_dir = rest.partition(" ")
                m = _get_msg(store, seq_str)
                if not m:
                    continue
                if not m.get("file_key"):
                    print(f"消息 #{m['seq']} 不是文件/图片类消息（类型: {m['msg_type']}）")
                    continue
                save_dir = save_dir or "downloads"
                os.makedirs(save_dir, exist_ok=True)
                data, name = feishu.download_file(m["message_id"], m["file_key"], m.get("res_type", "file"))
                out = os.path.join(save_dir, m.get("file_name") or name)
                with open(out, "wb") as f:
                    f.write(data)
                print(f"已下载 {len(data)} 字节 -> {out}")
            else:
                print(f"未知命令: {cmd}，输入 help 查看帮助")
        except Exception as e:
            print(f"执行失败: {e}")


def _cmd_list(store):
    msgs = store.list()
    if not msgs:
        print("（暂无消息，去飞书里给机器人发条消息试试）")
    for m in msgs:
        print(f"#{m['seq']} [{m['time']}] {m['sender_open_id']} ({m['msg_type']}): {m['content']}")


def _get_msg(store, seq_str):
    m = store.get(_to_int(seq_str))
    if not m:
        print(f"消息 #{seq_str} 不存在，先用 list 查看序号")
    return m


def _to_int(s) -> int | None:
    try:
        return int(str(s).strip().lstrip("#"))
    except ValueError:
        return None
