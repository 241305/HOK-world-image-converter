# -*- coding: utf-8 -*-
"""
GameAlbum（王者荣耀世界）APD / APT 图片转换工具
===========================================
功能：
  1. APD -> JPG      解开游戏相册全尺寸截图
  2. JPG/PNG -> APD  把普通图片打包成游戏相册格式（可同时生成 APT 缩略图）
  3. APT -> PNG      解开游戏相册缩略图
  4. PNG -> APT      把缩略图打包成 APT 格式

格式说明（逆向自 NGR/Saved/GameAlbum 下的真实文件）：
  - APD = 明文头部元数据 + uint32(JPEG长度) + 加密JPEG数据 [+ uint32(PNG长度) + 加密PNG缩略图]
  - APT = 加密的 PNG 缩略图（400x225）
  - 加密方式：固定 4 字节密钥循环 XOR（密钥 57 C4 3A 21，数据段起点相位归零）

运行：  python GameAlbum转换工具.py         （图形界面）
        python GameAlbum转换工具.py --cli apd2jpg xxx.APD    （命令行）

依赖： Pillow（图像处理）。安装：pip install Pillow
"""
import os
import sys
import shutil
import struct
import json
import random
import string
import time
import argparse
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
except Exception:
    TkinterDnD = None
    DND_FILES = None

# 启用 per-monitor DPI 感知：多显示器（如 4K 主屏 + 1080P 副屏）时，
# 窗口打开在哪块屏就按哪块屏的 DPI 渲染，避免字体/窗口大小错乱。
# 必须在任何窗口创建之前调用。
try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

# ---------------------------------------------------------------- 核心格式

KEY = b"\x57\xC4\x3A\x21"                      # 循环 XOR 密钥
MAGIC = b"GameAlbumHeader0.5"                   # 格式标识
ENC_JPEG_HEAD = b"\xA8\x1C\xC5\xC1"            # 加密后的 JPEG 头 (FF D8 FF E0 xor KEY)
TYPE_IMAGE = b"\x0F\x00\x00\x00"               # 图片类型标记
TAIL_THUMB_SIZE = (400, 225)                   # APT / 尾部缩略图尺寸


def xor_xform(data, key=KEY):
    """循环 XOR 变换（加密与解密是同一操作）"""
    out = bytearray(len(data))
    for i, b in enumerate(data):
        out[i] = b ^ key[i & 3]
    return bytes(out)


def _u32(v):
    return struct.pack("<I", v)


def _field(text):
    """头部字符串字段：uint32(长度含结尾0) + utf8 + 0x00"""
    if isinstance(text, str):
        text = text.encode("utf-8")
    return _u32(len(text) + 1) + text + b"\x00"


def _parse_fields(raw, start=4):
    """解析头部字符串字段序列，返回 (字段列表, 结束偏移)"""
    fields = []
    i = start
    while i + 4 <= len(raw):
        l = struct.unpack("<I", raw[i:i + 4])[0]
        i += 4
        if l == 0 or l > 512 or i + l > len(raw):
            return fields, i - 4
        s = raw[i:i + l]
        try:
            fields.append(s.rstrip(b"\x00").decode("utf-8"))
        except Exception:
            fields.append(s.hex())
        i += l
    return fields, i


def build_header(filename, role="", account="", icon="LandmarkIcon",
                 map_name="", share_code=None, jump_type=None):
    """
    组装 APD 明文头部。
    icon: 'LandmarkIcon'(简单型) 或 'CreditGotoHomeIcon'(家园拍照型，带分享码/JSON)
    """
    head = bytearray(TYPE_IMAGE)
    head += _u32(len(MAGIC) + 1) + MAGIC + b"\x00"     # 特殊 magic 字段（前缀含结尾 0）
    head += _field(filename)                            # 文件名（不含扩展名）
    head += _field("")                                  # 空
    head += _field(role if role else "未命名")           # 角色/昵称
    head += _field(account if account else "0")         # 账号 ID
    if icon == "CreditGotoHomeIcon":
        code = share_code or _gen_share_code()
        head += _field("家园拍照")                       # 截图类型标签
        head += _field("CreditGotoHomeIcon")            # 图标类型
        head += _field("")
        head += _field("分享码：" + code)
        head += _field(map_name if map_name else "未知地图")
        head += _field("1")
        head += _field(code)
    else:
        head += _field("")
        head += _field("LandmarkIcon")
        head += _field("")
        head += _field("")
        head += _field(map_name if map_name else "未知地图")
        head += _field("")
        head += _field("")
        head += _field("")
        head += _field("")
        head += _field("")
    return bytes(head)


def _gen_share_code():
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=12))


def _build_jump_json(filename, share_code, jump_type=85):
    """家园拍照型 APD 数据段前的 JSON 元数据（简化版）"""
    tid = int(time.time()) % 1000000000
    data = {
        "SystemJumpType": jump_type,
        "ShareCode": share_code,
        "SystemJumpArgTbl": {
            "ShareCode": share_code,
            "PhotoMode": 0,
            "TemplateInfo": {
                "templateID": tid,
                "postID": 0,
                "sourceType": 4,
                "shareCode": share_code,
                "templateData": "{}",
                "previewPhoto": str(tid),
                "roleID": 0,
                "templateName": "拍照模板",
                "templateDesc": "模板描述",
            },
        },
    }
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def make_thumbnail(img, size=TAIL_THUMB_SIZE):
    """居中裁剪后缩放到目标尺寸"""
    from PIL import Image
    w, h = img.size
    tr = size[0] / size[1]
    sr = w / h
    if sr > tr:
        nw = int(h * tr)
        x = (w - nw) // 2
        img = img.crop((x, 0, x + nw, h))
    else:
        nh = int(w / tr)
        y = (h - nh) // 2
        img = img.crop((0, y, w, y + nh))
    return img.resize(size, Image.LANCZOS)


# ---------------------------------------------------------------- 解包方向

def _locate_jpeg_seg(raw):
    """可靠定位 APD 的 JPEG 段。返回 (jpeg段偏移, 头部字节)。
    头部字段与 JPEG 段均为「uint32长度+数据」结构，从头逐段遍历，解密后以 FF D8 开头即为 JPEG 段。
    兼容两种加密 JPEG 头：游戏原生（JFIF APP0 → A81CC5C1）与工具生成（PIL 无 APP0 → A81CC5FA）。"""
    p = 4
    while p + 4 <= len(raw):
        jl = struct.unpack("<I", raw[p:p + 4])[0]
        if not (0 < jl <= len(raw) - p - 4):
            break
        dec = xor_xform(raw[p + 4:p + 4 + min(jl, 16)])
        if dec.startswith(b"\xFF\xD8") and jl > 1000:
            return p, raw[:p]
        p = p + 4 + jl
    # 兜底：按特征字节搜索，且必须满足「长度合理 + 解密后是 JPEG」双重校验
    for sig in (ENC_JPEG_HEAD, b"\xA8\x1C\xC5\xFA"):
        pos = raw.find(sig)
        while pos >= 0:
            if pos >= 4:
                jl = struct.unpack("<I", raw[pos - 4:pos])[0]
                if 0 < jl <= len(raw) - pos:
                    dec = xor_xform(raw[pos:pos + min(jl, 16)])
                    if dec.startswith(b"\xFF\xD8"):
                        return pos - 4, raw[:pos - 4]
            nxt = raw.find(sig, pos + 1)
            if nxt == pos:
                break
            pos = nxt
    raise ValueError("未找到 JPEG 数据段（文件可能不是 GameAlbum 图片 APD）")


def _patch_header_field(raw, idx, new_val):
    """把 APD 明文头部第 idx 个字段替换为新字符串（二进制级，其余字节原样）。"""
    pos = 4
    for i in range(idx + 1):
        if pos + 4 > len(raw):
            raise ValueError("头部字段不足")
        ln = struct.unpack("<I", raw[pos:pos + 4])[0]
        pos += 4
        if i == idx:
            if pos + ln > len(raw):
                raise ValueError("头部字段越界")
            nb = new_val.encode("utf-8")
            return raw[:pos - 4] + _u32(len(nb)) + nb + raw[pos + ln:]
        pos += ln
    raise ValueError("头部字段索引超出")


def make_apd_from_template(template_apd, image_path, out_dir=None, filename=None,
                           quality=100, make_apt=True, add_tail=True):
    """用游戏原生 APD 作模板：完整复制其头部（含真实分享码/JSON/昵称/账号/地图），
    只替换主图与尾部缩略图，生成新的 APD/APT。游戏内可正常识别、点开清晰。
    返回 (新APD路径, 新APT路径或None)"""
    from PIL import Image
    import io
    raw = open(template_apd, "rb").read()
    jpos, head_bytes = _locate_jpeg_seg(raw)
    jl = struct.unpack("<I", raw[jpos:jpos + 4])[0]
    old_tail = raw[jpos + 4 + jl:]

    jpeg, img = _image_to_jpeg(image_path, quality)

    fields, _, _, _ = parse_apd(template_apd)
    acct = fields[4] if len(fields) > 4 and fields[4] else "0"
    if not filename:
        filename = "{}_{}".format(acct, int(time.time() * 1000))
    # 头部文件名字段[1] 必须与磁盘文件名一致，否则游戏定位不到主图、点开会回退缩略图变糊
    head_bytes = _patch_header_field(head_bytes, 1, filename)

    # APT 缩略图：新图的缩略图（尺寸沿用模板尾部尺寸），供游戏列表显示
    tail_png_size = None
    if len(old_tail) >= 4:
        tl = struct.unpack("<I", old_tail[:4])[0]
        if 0 < tl <= len(old_tail) - 4:
            cand = xor_xform(old_tail[4:4 + tl])
            if cand.startswith(b"\x89PNG"):
                try:
                    tail_png_size = Image.open(io.BytesIO(cand)).size
                except Exception:
                    pass
    png = _png_bytes(make_thumbnail(img, size=tail_png_size or TAIL_THUMB_SIZE))

    # 尾部：与「替换 APD 图片」行为一致——默认保留模板原尾部，只换主图。
    # （游戏可能校验尾部与内部记录的匹配，换成新缩略图会导致点开回退缩略图变糊）
    new_tail = old_tail

    out_dir = out_dir or os.path.dirname(image_path)
    os.makedirs(out_dir, exist_ok=True)
    out_apd = os.path.join(out_dir, filename + ".APD")
    with open(out_apd, "wb") as f:
        f.write(head_bytes + _u32(len(jpeg)) + xor_xform(jpeg) + new_tail)

    out_apt = None
    if make_apt:
        out_apt = png_to_apt(png, out_dir, name=filename + ".APT")
    return out_apd, out_apt


def parse_apd(path):
    """解析 APD，返回 (头部字段列表, JPEG字节, 尾部PNG字节或None, 尾部加密PNG字节或None)"""
    raw = open(path, "rb").read()
    fields, _ = _parse_fields(raw)
    jpos, _ = _locate_jpeg_seg(raw)
    jl = struct.unpack("<I", raw[jpos:jpos + 4])[0]
    if not (0 < jl <= len(raw) - jpos - 4):
        raise ValueError("JPEG 长度字段异常，文件可能已损坏")
    jpeg = xor_xform(raw[jpos + 4:jpos + 4 + jl])
    after = jpos + 4 + jl
    # 尾部：uint32 长度 + 加密 PNG（LandmarkIcon 型）
    tail_png = None
    tail_raw = None
    rest = raw[after:]
    if len(rest) >= 4:
        tl = struct.unpack("<I", rest[:4])[0]
        if 0 < tl <= len(rest) - 4:
            cand = rest[4:4 + tl]
            if xor_xform(cand).startswith(b"\x89PNG"):
                tail_png = xor_xform(cand)
                tail_raw = cand
    return fields, jpeg, tail_png, tail_raw


def apd_to_images(apd_path, out_dir=None, export_tail=True, export_png=False):
    """APD -> JPG（+ 可选导出尾部缩略图 PNG / 主图 PNG）。返回输出文件列表"""
    base = os.path.splitext(os.path.basename(apd_path))[0]
    out_dir = out_dir or os.path.dirname(apd_path)
    os.makedirs(out_dir, exist_ok=True)
    fields, jpeg, tail_png, _ = parse_apd(apd_path)
    jpg_path = os.path.join(out_dir, base + ".jpg")
    with open(jpg_path, "wb") as f:
        f.write(jpeg)
    results = [jpg_path]
    if export_png and jpeg:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(jpeg))
        png_path = os.path.join(out_dir, base + ".png")
        img.save(png_path, "PNG")
        results.append(png_path)
    if export_tail and tail_png:
        png_path = os.path.join(out_dir, base + "_thumb.png")
        with open(png_path, "wb") as f:
            f.write(tail_png)
        results.append(png_path)
    return results, fields


def apd_to_png(apd_path, out_dir=None, export_tail=True, export_jpg=False):
    """APD -> PNG（主图转为 PNG）。返回输出文件列表"""
    base = os.path.splitext(os.path.basename(apd_path))[0]
    out_dir = out_dir or os.path.dirname(apd_path)
    os.makedirs(out_dir, exist_ok=True)
    fields, jpeg, tail_png, _ = parse_apd(apd_path)
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(jpeg))
    png_path = os.path.join(out_dir, base + ".png")
    img.save(png_path, "PNG")
    results = [png_path]
    if export_jpg:
        jpg_path = os.path.join(out_dir, base + ".jpg")
        with open(jpg_path, "wb") as f:
            f.write(jpeg)
        results.append(jpg_path)
    if export_tail and tail_png:
        png_path2 = os.path.join(out_dir, base + "_thumb.png")
        with open(png_path2, "wb") as f:
            f.write(tail_png)
        results.append(png_path2)
    return results, fields


def apt_to_png(apt_path, out_dir=None, name=None):
    """APT -> PNG（返回输出文件路径）"""
    raw = open(apt_path, "rb").read()
    dec = xor_xform(raw)
    if not dec.startswith(b"\x89PNG"):
        # 有些 APT 可能内部是 JPEG
        s = dec.find(b"\xFF\xD8\xFF")
        e = dec.rfind(b"\xFF\xD9")
        if s >= 0 and e > s:
            ext = ".jpg"
            data = dec[s:e + 2]
        else:
            raise ValueError("APT 解密后不是有效 PNG/JPEG")
    else:
        ext = ".png"
        data = dec
    base = os.path.splitext(os.path.basename(apt_path))[0]
    out_dir = out_dir or os.path.dirname(apt_path)
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, name or base + ext)
    with open(out, "wb") as f:
        f.write(data)
    return out


# ---------------------------------------------------------------- 打包方向

def _image_to_jpeg(image_path, quality=100):
    """打开图片并转为 JPEG 字节。输入本身就是 JPEG 时直接复用原字节（无损）。"""
    from PIL import Image
    import io
    img = Image.open(image_path)
    if img.mode != "RGB":
        img = img.convert("RGB")
    ext = os.path.splitext(image_path)[1].lower()
    if ext in (".jpg", ".jpeg"):
        jpeg = open(image_path, "rb").read()
    else:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality)
        jpeg = buf.getvalue()
    return jpeg, img


def _png_bytes(thumb):
    import io
    buf = io.BytesIO()
    thumb.save(buf, "PNG")
    return buf.getvalue()


def image_to_apd(image_path, out_dir=None, filename=None, role="", account="",
                 icon="LandmarkIcon", map_name="", share_code=None, jump_type=85,
                 quality=100, make_apt=True, add_tail=True):
    """图片 -> APD（可同时生成 APT 缩略图）。返回 (APD路径, APT路径或None)"""
    from PIL import Image

    out_dir = out_dir or os.path.dirname(image_path)
    os.makedirs(out_dir, exist_ok=True)

    # JPEG 主图：输入本身就是 JPEG 时直接用原字节（无损），否则转 JPEG
    jpeg, img = _image_to_jpeg(image_path, quality)

    if not filename:
        acct = account if account else "0"
        filename = "{}_{}".format(acct, int(time.time() * 1000))

    head = build_header(filename, role=role, account=account,
                        icon=icon, map_name=map_name, share_code=share_code)

    body = bytearray()
    if icon == "CreditGotoHomeIcon":
        js = _build_jump_json(filename, share_code or _gen_share_code(), jump_type)
        body += _u32(len(js)) + js
    body += _u32(len(jpeg)) + xor_xform(jpeg)

    # 尾部缩略图（LandmarkIcon 型附加）
    if add_tail:
        png = _png_bytes(make_thumbnail(img))
        body += _u32(len(png)) + xor_xform(png)

    apd_path = os.path.join(out_dir, filename + ".APD")
    with open(apd_path, "wb") as f:
        f.write(bytes(head) + bytes(body))

    # 可选同时生成 APT 缩略图
    apt_path = None
    if make_apt:
        apt_path = png_to_apt(png if add_tail else _make_thumb_bytes(img),
                              out_dir, name=filename + ".APT")
    return apd_path, apt_path


def _make_thumb_bytes(img):
    return _png_bytes(make_thumbnail(img))


def replace_apd_image(apd_path, image_path, out_dir=None, quality=100,
                      update_tail=False, update_apt=True, src_bytes=None):
    """
    在现有 APD 基础上只替换图片，其余全部保留：
      - 头部元数据（账号ID/昵称/地图/图标类型/分享码/JSON）原样保留
      - 文件名不变
      - 只替换 JPEG 主图数据段（更新长度字段）
      - 尾部：默认原样保留（其他内容不变）；若尾部是可解析的加密 PNG 缩略图
        且 update_tail=True，则沿用原缩略图尺寸同步换成新图的缩略图
      - 若原目录存在同名 APT 且 update_apt，沿用原 APT 尺寸同步更新缩略图
    返回 (新APD路径, 新APT路径或None, 头部字段列表)
    """
    if src_bytes is not None:
        raw = src_bytes
    else:
        raw = open(apd_path, "rb").read()
    # 可靠定位 JPEG 段：从头部字段结束处按结构核对，避免加密数据里的特征字节伪匹配
    jpos, head_bytes = _locate_jpeg_seg(raw)
    jl = struct.unpack("<I", raw[jpos:jpos + 4])[0]
    if not (0 < jl <= len(raw) - jpos - 4):
        raise ValueError("JPEG 长度字段异常，文件可能已损坏")

    old_tail = raw[jpos + 4 + jl:]              # 原尾部

    # 判断尾部是否为"长度+加密PNG"的可解析结构（LandmarkIcon / 家园拍照型一致）
    tail_is_png = False
    tail_png_size = None
    if len(old_tail) >= 4:
        tl = struct.unpack("<I", old_tail[:4])[0]
        if 0 < tl <= len(old_tail) - 4:
            cand = old_tail[4:4 + tl]
            dec_cand = xor_xform(cand)
            if dec_cand.startswith(b"\x89PNG"):
                tail_is_png = True
                try:
                    from PIL import Image
                    import io
                    tail_png_size = Image.open(io.BytesIO(dec_cand)).size
                except Exception:
                    tail_png_size = None

    jpeg, img = _image_to_jpeg(image_path, quality)

    new_tail = old_tail
    if update_tail and tail_is_png:
        # 沿用原尾部缩略图的尺寸生成新缩略图
        png = _png_bytes(make_thumbnail(img, size=tail_png_size or TAIL_THUMB_SIZE))
        new_tail = _u32(len(png)) + xor_xform(png)

    out_bytes = head_bytes + _u32(len(jpeg)) + xor_xform(jpeg) + new_tail

    base = os.path.splitext(os.path.basename(apd_path))[0]
    out_dir = out_dir or os.path.dirname(apd_path)
    os.makedirs(out_dir, exist_ok=True)
    out_apd = os.path.join(out_dir, base + ".APD")          # 文件名不变
    with open(out_apd, "wb") as f:
        f.write(out_bytes)

    out_apt = None
    orig_apt = os.path.join(os.path.dirname(apd_path), base + ".APT")
    if update_apt and os.path.exists(orig_apt):
        # 沿用原 APT 缩略图的尺寸生成新缩略图
        apt_size = TAIL_THUMB_SIZE
        try:
            from PIL import Image
            import io
            apt_dec = xor_xform(open(orig_apt, "rb").read())
            if apt_dec.startswith(b"\x89PNG"):
                apt_size = Image.open(io.BytesIO(apt_dec)).size
        except Exception:
            pass
        png = _png_bytes(make_thumbnail(img, size=apt_size))
        out_apt = os.path.join(out_dir, base + ".APT")
        with open(out_apt, "wb") as f:
            f.write(xor_xform(png))

    fields, _, _, _ = parse_apd(out_apd)
    return out_apd, out_apt, fields


def png_to_apt(png_path_or_bytes, out_dir=None, name=None, keep_size=False):
    """PNG -> APT（默认缩放到 400x225）。返回输出文件路径"""
    from PIL import Image
    import io

    if isinstance(png_path_or_bytes, (bytes, bytearray)):
        src_bytes = bytes(png_path_or_bytes)
        img = Image.open(io.BytesIO(src_bytes))
        src_name = "转换输出"
        src_is_png = img.format == "PNG"
    else:
        src_bytes = open(png_path_or_bytes, "rb").read()
        img = Image.open(io.BytesIO(src_bytes))
        src_name = os.path.splitext(os.path.basename(png_path_or_bytes))[0]
        src_is_png = img.format == "PNG"
    if img.mode != "RGB":
        img = img.convert("RGB")

    # 已经是目标尺寸的 PNG：原样使用字节（无损）
    if src_is_png and (keep_size or img.size == TAIL_THUMB_SIZE):
        png = src_bytes
    else:
        if not keep_size and img.size != TAIL_THUMB_SIZE:
            img = make_thumbnail(img, TAIL_THUMB_SIZE)
        buf = os.path.join(out_dir or ".", "_tmp_a.png")
        img.save(buf, "PNG")
        png = open(buf, "rb").read()
        os.remove(buf)

    out_dir = out_dir or os.path.dirname(
        png_path_or_bytes if isinstance(png_path_or_bytes, str) else ".")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, name or src_name + ".APT")
    with open(out, "wb") as f:
        f.write(xor_xform(png))
    return out


# ---------------------------------------------------------------- 命令行入口

def _cli():
    p = argparse.ArgumentParser(description="GameAlbum APD/APT 图片转换工具")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("apd2jpg", help="APD -> JPG"); a.add_argument("input"); a.add_argument("-o", "--outdir")
    a.add_argument("--png", action="store_true", help="同时输出 PNG 主图")
    a.add_argument("--no-tail", action="store_true", help="不导出尾部缩略图")
    f = sub.add_parser("apd2png", help="APD -> PNG"); f.add_argument("input"); f.add_argument("-o", "--outdir")
    f.add_argument("--jpg", action="store_true", help="同时输出 JPG 主图")
    f.add_argument("--no-tail", action="store_true", help="不导出尾部缩略图")
    b = sub.add_parser("apt2png", help="APT -> PNG"); b.add_argument("input"); b.add_argument("-o", "--outdir")
    c = sub.add_parser("jpg2apd", help="图片 -> APD"); c.add_argument("input"); c.add_argument("-o", "--outdir")
    c.add_argument("--account", default=""); c.add_argument("--role", default="")
    c.add_argument("--map", default=""); c.add_argument("--icon", default="LandmarkIcon",
                                                         choices=["LandmarkIcon", "CreditGotoHomeIcon"])
    c.add_argument("--no-apt", action="store_true"); c.add_argument("--no-tail", action="store_true")
    d = sub.add_parser("png2apt", help="PNG -> APT"); d.add_argument("input"); d.add_argument("-o", "--outdir")
    e = sub.add_parser("replace", help="替换 APD 中的图片（保留原有信息与文件名）")
    e.add_argument("apd"); e.add_argument("image"); e.add_argument("-o", "--outdir")
    e.add_argument("--no-apt", action="store_true", help="不同步更新同名 APT 缩略图")
    args = p.parse_args()
    try:
        if args.cmd == "apd2jpg":
            out, fields = apd_to_images(args.input, args.outdir, export_png=args.png,
                                        export_tail=not args.no_tail)
            print("导出:", out); print("头部信息:", fields)
        elif args.cmd == "apd2png":
            out, fields = apd_to_png(args.input, args.outdir, export_jpg=args.jpg,
                                     export_tail=not args.no_tail)
            print("导出:", out); print("头部信息:", fields)
        elif args.cmd == "apt2png":
            print("导出:", apt_to_png(args.input, args.outdir))
        elif args.cmd == "jpg2apd":
            apd, apt = image_to_apd(args.input, args.outdir, account=args.account,
                                    role=args.role, map_name=args.map, icon=args.icon,
                                    make_apt=not args.no_apt, add_tail=not args.no_tail)
            print("APD:", apd, " APT:", apt)
        elif args.cmd == "png2apt":
            print("导出:", png_to_apt(args.input, args.outdir))
        elif args.cmd == "replace":
            apd, apt, fields = replace_apd_image(args.apd, args.image, args.outdir,
                                                 update_apt=not args.no_apt)
            print("替换完成 APD:", apd, " APT:", apt)
            print("保留的头部信息:", fields)
    except Exception as ex:
        print("错误:", ex); sys.exit(1)


# ---------------------------------------------------------------- 图形界面

def _run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    # Windows 高 DPI 修复：先声明进程 DPI 感知（必须在创建 Tk 之前），
    # 否则系统会对整个窗口做位图缩放，4K + 系统缩放下界面会模糊
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # system DPI aware
    except Exception:
        pass

    if TkinterDnD is not None:
        try:
            root = TkinterDnD.Tk()   # 支持系统级文件拖放
        except Exception:
            root = tk.Tk()
    else:
        root = tk.Tk()
    # 按窗口所在显示器的 DPI 调整 tkinter 内部缩放，并让窗口尺寸跟随该显示器分辨率
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        shcore = ctypes.windll.shcore

        class _RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        class _MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _RECT),
                        ("rcWork", _RECT), ("dwFlags", ctypes.c_ulong)]

        hmon = None
        try:
            hwnd = root.winfo_id()
            hmon = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
            d = wintypes.DWORD()
            shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(d), ctypes.byref(d))
            dpi = d.value
        except Exception:
            dpi = user32.GetDpiForSystem()
        scale = max(1.0, dpi / 96.0)
        root.tk.call('tk', 'scaling', scale)

        # 该显示器物理分辨率 → 逻辑尺寸
        try:
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(mi)
            if hmon and user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                log_w = int((mi.rcMonitor.right - mi.rcMonitor.left) / scale)
                log_h = int((mi.rcMonitor.bottom - mi.rcMonitor.top) / scale)
            else:
                raise ValueError
        except Exception:
            log_w = int(user32.GetSystemMetrics(0) / scale)
            log_h = int(user32.GetSystemMetrics(1) / scale)

        # 固定窗口“视觉大小”（逻辑像素 DIP 恒定）：基准为 4K 屏 150% 缩放下窗口 1200x900 物理像素
        # （= 800x600 DIP）。任何分辨率的屏幕上打开，窗口看起来一样大；
        # 仅当当前屏幕放不下时自动收窄，保证完整可见。
        DIP_W, DIP_H = 800, 600
        win_w = int(DIP_W * scale)
        win_h = int(DIP_H * scale)
        if win_w > log_w: win_w = int(log_w * 0.94)
        if win_h > log_h: win_h = int(log_h * 0.94)
        root.geometry("{}x{}".format(win_w, win_h))
    except Exception:
        pass
    root.title("王者世界相册图片转换工具 v1.1")
    # 程序图标（窗口标题栏 / 任务栏）：优先用打包进 EXE 的 icon.ico
    try:
        _icon_path = os.path.join(
            getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "icon.ico")
        if os.path.exists(_icon_path):
            root.iconbitmap(_icon_path)
    except Exception:
        pass

    # ---- 配色与字体（现代化深色主题） ----
    BG, CARD, CARD2, DEEP = "#0A0F1E", "#141F36", "#1C2B4A", "#0D1628"
    TXT, SUB = "#E8EEF9", "#8395BE"
    BTN, BTN_H, ACC = "#2563EB", "#3B82F6", "#4C8DFF"
    OK, OK_H = "#16A34A", "#22C55E"
    BORDER = "#2A3C60"   # 统一控件边框（卡片 / 输入框 / 悬浮提示）
    SEL_BG = "#1B3A63"   # 功能卡片选中时的底色
    F = ("Microsoft YaHei UI", 12)
    F_B = ("Microsoft YaHei UI", 12, "bold")
    root.configure(bg=BG)
    root.minsize(700, 520)

    # ---- 主内容滚动容器：窗口缩小到放不下时出现滚动条，滚轮查看全部内容 ----
    main_canvas = tk.Canvas(root, bg=BG, highlightthickness=0)

    class SlimScrollbar(tk.Canvas):
        """细条式现代滚动条：深色轨道 + 圆角滑块 + 悬停高亮，支持拖动与翻页"""

        def __init__(self, master, command, **kw):
            kw.setdefault("width", 10)
            super().__init__(master, bg=BG, highlightthickness=0,
                             bd=0, relief="flat", cursor="hand2", **kw)
            self._cmd = command
            self._frac = (0.0, 1.0)
            self._drag_y = None
            self._hovering = False
            self.bind("<Button-1>", self._press)
            self.bind("<B1-Motion>", self._drag)
            self.bind("<ButtonRelease-1>", self._release)
            self.bind("<Enter>", lambda e: self._set_hover(True))
            self.bind("<Leave>", lambda e: self._set_hover(False))

        def set(self, lo, hi):
            try:
                self._frac = (max(0.0, min(1.0, float(lo))), max(0.0, min(1.0, float(hi))))
            except Exception:
                self._frac = (0.0, 1.0)
            self._redraw()

        def _set_hover(self, on):
            self._hovering = on
            self._redraw()

        def _redraw(self):
            self.delete("all")
            h = self.winfo_height()
            w = self.winfo_width()
            if h <= 0 or w <= 0:
                return
            self.create_rectangle(0, 0, w, h, fill=BG, outline="")
            lo, hi = self._frac
            top = int(lo * h)
            bot = int(hi * h)
            if bot - top < 28:
                bot = min(h, top + 28)
            if top >= bot or hi <= lo:
                return
            cx = w / 2.0
            rw = max(3.0, w - 6)
            color = "#3B82F6" if (self._hovering or self._drag_y is not None) else "#475569"
            x0, x1 = cx - rw / 2, cx + rw / 2
            self.create_oval(x0, top, x1, top + rw, fill=color, outline="")
            self.create_rectangle(x0, top + rw / 2, x1, bot - rw / 2, fill=color, outline="")
            self.create_oval(x0, bot - rw, x1, bot, fill=color, outline="")

        def _press(self, e):
            lo, hi = self._frac
            h = self.winfo_height()
            if h <= 0 or hi <= lo:
                return
            top, bot = lo * h, hi * h
            if top <= e.y <= bot:
                self._drag_y = e.y - top
            else:
                self._cmd("scroll", -1 if e.y < top else 1, "pages")

        def _drag(self, e):
            h = self.winfo_height()
            if h <= 0 or self._drag_y is None:
                return
            lo, hi = self._frac
            page = max(hi - lo, 0.1)
            target = (e.y - self._drag_y) / h
            target = max(0.0, min(1.0 - page, target))
            self._cmd("moveto", str(target))

        def _release(self, _e):
            self._drag_y = None
            self._redraw()

    vbar = SlimScrollbar(root, command=main_canvas.yview)
    main_canvas.configure(yscrollcommand=vbar.set)
    main_canvas.grid(row=0, column=0, sticky="nsew")
    vbar.grid(row=0, column=1, sticky="ns")
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)

    main_frame = tk.Frame(main_canvas, bg=BG)
    _win_id = main_canvas.create_window((0, 0), window=main_frame, anchor="nw")
    main_frame.bind("<Configure>",
                    lambda e: main_canvas.configure(scrollregion=(0, 0, e.width, e.height)))
    main_canvas.bind("<Configure>",
                     lambda e: main_canvas.itemconfigure(_win_id, width=e.width))

    # ---- 按住左键拖动内容区垂直滚动（抓手 pan，只滚上下不左右移）；按钮/输入框等不触发 ----
    _NO_PAN = ("Button", "TButton", "Entry", "TEntry", "Combobox", "TCombobox",
               "Text", "Listbox", "Treeview", "Spinbox")
    _pan = {"mode": None, "y0": 0.0, "f0": 0.0, "total": 1.0}

    def _pan_start(e):
        if e.widget.winfo_class() in _NO_PAN:
            return
        # 鼠标在说明窗口内 → 说明窗垂直 pan
        try:
            hw = help_win.get("w")
            hc = help_win.get("cv")
            hi = help_win.get("inner")
            if hw is not None and hc is not None and hi is not None and hw.winfo_exists():
                wx, wy = hw.winfo_rootx(), hw.winfo_rooty()
                ww, wh = hw.winfo_width(), hw.winfo_height()
                if wx <= e.x_root <= wx + ww and wy <= e.y_root <= wy + wh:
                    _pan["mode"] = "h"
                    _pan["y0"] = e.y_root
                    _pan["f0"] = hc.yview()[0]
                    _pan["total"] = hi.winfo_height() or 1
                    return
        except Exception:
            pass
        # 否则主菜单垂直 pan
        _pan["mode"] = "m"
        _pan["y0"] = e.y_root
        _pan["f0"] = main_canvas.yview()[0]
        _pan["total"] = main_frame.winfo_height() or 1
        main_canvas.configure(cursor="hand2")

    def _pan_move(e):
        dy = e.y_root - _pan["y0"]
        total = _pan["total"] or 1
        if _pan["mode"] == "h":
            hc = help_win.get("cv")
            if hc is not None:
                hc.yview_moveto(max(0.0, min(1.0, _pan["f0"] - dy / total)))
        elif _pan["mode"] == "m":
            main_canvas.yview_moveto(max(0.0, min(1.0, _pan["f0"] - dy / total)))

    def _pan_end(_e):
        if _pan["mode"] == "m":
            main_canvas.configure(cursor="")
        _pan["mode"] = None

    root.bind_all("<ButtonPress-1>", _pan_start)
    root.bind_all("<B1-Motion>", _pan_move)
    root.bind_all("<ButtonRelease-1>", _pan_end)

    def _on_wheel(e):
        """滚轮：鼠标在说明窗口内滚说明窗，否则滚主内容；自带滚动的控件自己滚"""
        # 说明窗口打开且鼠标在其范围内 → 滚动说明窗
        try:
            hw = help_win.get("w")
            hc = help_win.get("cv")
            if hw is not None and hc is not None and hw.winfo_exists():
                wx, wy = hw.winfo_rootx(), hw.winfo_rooty()
                ww, wh = hw.winfo_width(), hw.winfo_height()
                if wx <= e.x_root <= wx + ww and wy <= e.y_root <= wy + wh:
                    hc.yview_scroll(int(-e.delta / 120) * 2, "units")
                    return
        except Exception:
            pass
        try:
            w = root.winfo_containing(e.x_root, e.y_root)
        except Exception:
            w = None
        if w is not None:
            cls = w.winfo_class()
            # 日志 Text 也允许滚轮滚主菜单（日志只由自己的滑条拖动）
            if isinstance(w, (tk.Listbox,)) or cls in ("TCombobox", "Listbox", "Treeview"):
                return
        main_canvas.yview_scroll(int(-e.delta / 120) * 2, "units")
    root.bind_all("<MouseWheel>", _on_wheel)

    # ---- 标题区 ----
    head = tk.Frame(main_frame, bg=BG)
    head.grid(row=0, column=0, columnspan=5, pady=(16, 2))
    tk.Label(head, text="王者世界相册图片转换工具", bg=BG, fg="#F8FAFC",
             font=("Microsoft YaHei UI", 20, "bold")).pack(side="left", padx=(6, 0))
    tk.Label(main_frame, text="王者荣耀世界 APD / APT 相册格式互转 · 支持图片替换与缩略图同步更新",
             bg=BG, fg=SUB, font=("Microsoft YaHei UI", 11)).grid(row=1, column=0, columnspan=5, pady=(0, 10))

    # ---- 左上角：打开游戏相册目录（固定指向游戏相册路径，不随输入框修改） ----
    def open_game_album():
        d = _game_album_path()
        if os.path.isdir(d):
            os.startfile(d)
            say("已打开游戏相册目录：" + d)
        else:
            messagebox.showwarning("目录不存在",
                                   "游戏相册目录不存在：\n{}\n\n请先进入游戏截图生成相册，或检查目录设置。".format(d))

    btn_help = tk.Button(main_frame, text="使用说明", command=lambda: toggle_help(), bg="#1E3A5F",
                          fg="#93C5FD", activebackground="#1D4ED8", activeforeground="white",
                          relief="flat", bd=0, padx=14, pady=4, font=F,
                          cursor="hand2", highlightthickness=0)
    btn_help.place(x=20, y=18, anchor="nw")

    # ---- 免责声明（并入「使用说明」窗口底部；右上角按钮打开说明窗口并滚到底） ----
    DISCLAIMER_TEXT = (
        "本工具通过逆向游戏文件格式开发，仅供学习与个人娱乐使用，请勿用于商业用途或传播侵权内容。\n\n"
        "游戏版本更新后，文件格式或加密密钥可能发生变化，导致本工具失效或转换结果异常，不保证长期可用，也无法对后续游戏版本兼容性负责。\n\n"
        "操作前请自行备份游戏文件；因使用本工具造成的数据丢失、文件损坏、账号异常等问题，作者不承担任何责任。\n\n"
        "修改游戏本地文件可能违反游戏用户协议，请自行评估风险后再使用。"
    )

    def _goto_disclaimer():
        toggle_help()
        root.after(150, lambda: help_win["cv"].yview_moveto(1.0))

    btn_disclaim = tk.Button(main_frame, text="免责声明", command=_goto_disclaimer, bg="#7F1D1D",
                             fg="#FECACA", activebackground="#B91C1C", activeforeground="white",
                             relief="flat", bd=0, padx=14, pady=4, font=F,
                             cursor="hand2", highlightthickness=0)
    btn_disclaim.place(relx=1.0, x=-20, y=18, anchor="ne")

    # ---- 参数变量（供各功能面板使用） ----
    var_account = tk.StringVar(value="")
    # 图标类型固定为地标截图（LandmarkIcon）；勾选模板时自动跟随模板的类型
    _tpl_cb_ref = [None]
    _ACCT_PH = "使用游戏原生截图模板此ID无效"  # 勾模板时账号ID框内的占位提示
    _acct_eb_ref = [None]      # 账号ID输入框
    _acct_saved = [None]       # 勾模板前账号ID的原值（取消模板时恢复）
    var_apt = tk.BooleanVar(value=True)
    var_tail = tk.BooleanVar(value=False)
    var_tail_upd = tk.BooleanVar(value=False)
    var_png = tk.BooleanVar(value=False)
    var_jpg = tk.BooleanVar(value=False)
    var_tail_out = tk.BooleanVar(value=False)
    var_apt_upd = tk.BooleanVar(value=True)
    var_backup = tk.BooleanVar(value=True)
    var_copy_game = tk.BooleanVar(value=True)
    var_game_dir = tk.StringVar(value=os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "NGR", "Saved", "GameAlbum"))
    var_template = tk.BooleanVar(value=False)
    var_template_path = tk.StringVar(value="")
    var_keep_name = tk.BooleanVar(value=False)
    var_no_local = tk.BooleanVar(value=False)   # 仅复制到游戏相册，不在原路径生成
    var_quality5 = tk.IntVar(value=100)          # ⑤ 图片→APD 的压缩质量
    var_quality6 = tk.IntVar(value=100)          # ⑥ 替换 APD 的压缩质量
    _no_local_cb_ref = [None]                    # 该选项的复选框（供联动禁用）

    def _quality(var):
        """读取压缩质量，非法输入（手动填非数字/超范围）时回退默认 100"""
        try:
            q = int(var.get())
            return min(max(q, 1), 100)
        except Exception:
            return 100

    def _q_validate(p):
        """Spinbox 按键校验：只允许 1-100 的纯数字（空串暂放行，转换时兜底回退 100）"""
        if p == "":
            return True
        if not p.isdigit():
            return False
        return int(p) <= 100

    _q_vcmd = (root.register(_q_validate), "%P")

    # ---- 设置持久化：勾选状态 / 账号ID / 路径 重启后自动恢复 ----
    # 配置文件保存在程序（EXE）同目录，方便查看与备份
    _SETTINGS_FILE = os.path.join(
        os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)),
        "游戏相册转换工具_settings.json")
    # 说明窗口大小记忆：必须在 _save_settings 之前声明，Python 闭包才能正确捕获
    _help_geo_saved = [None]
    _SETTINGS_VARS = [
        ("var_template", var_template), ("var_template_path", var_template_path),
        ("var_account", var_account), ("var_apt", var_apt),
        ("var_tail", var_tail), ("var_tail_upd", var_tail_upd), ("var_png", var_png),
        ("var_jpg", var_jpg),
        ("var_tail_out", var_tail_out), ("var_apt_upd", var_apt_upd),
        ("var_backup", var_backup),
        ("var_copy_game", var_copy_game), ("var_game_dir", var_game_dir),
        ("var_keep_name", var_keep_name),
        ("var_no_local", var_no_local),
        ("var_quality5", var_quality5), ("var_quality6", var_quality6),
    ]

    def _save_settings(*_):
        """任何选项/输入变化时写入配置文件（失败静默，不影响使用）"""
        try:
            data = {n: v.get() for n, v in _SETTINGS_VARS}
            # 勾模板时账号ID框内显示的是占位提示，保存时还原为空（避免脏数据写入配置）
            if data.get("var_account") == _ACCT_PH:
                data["var_account"] = ""
            data["_last_dirs"] = _last_dirs  # 各功能区独立记忆的上次目录
            # 记忆主窗口与说明窗口大小
            try:
                data["_main_geo"] = root.geometry()
                if _help_geo_saved[0]:
                    data["_help_geo"] = _help_geo_saved[0]
            except Exception:
                pass
            with open(_SETTINGS_FILE, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def _load_settings():
        """启动时读取配置文件，恢复上次的勾选与输入"""
        try:
            with open(_SETTINGS_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            return
        for n, v in _SETTINGS_VARS:
            if n in data:
                try:
                    if n == "var_game_dir":
                        # 保存的游戏相册目录在本机不存在时，自动改成本机的默认路径
                        # （发给别人时，即使带配置也能自适应到对方的游戏相册目录）
                        d = data[n]
                        if not (isinstance(d, str) and d.strip() and os.path.isdir(d)):
                            v.set(os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                                               "NGR", "Saved", "GameAlbum"))
                            continue
                    v.set(data[n])
                except Exception:
                    pass
        _last_dirs.update(data.get("_last_dirs") or {})
        # 恢复主窗口大小（在屏幕范围内才用，否则用默认 DPI 自适应）
        try:
            mg = data.get("_main_geo")
            if isinstance(mg, str) and "x" in mg:
                w, h = (int(x) for x in mg.split("+")[0].split("x"))
                sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
                if 400 <= w <= sw and 300 <= h <= sh:
                    root.geometry(mg)
        except Exception:
            pass
        _help_geo_saved[0] = data.get("_help_geo") if isinstance(data.get("_help_geo"), str) else None

    for _, v in _SETTINGS_VARS:
        v.trace_add("write", _save_settings)
    def _template_changed(*_):
        # 勾模板时账号ID以模板为准：框内显示红色占位提示，取消模板时恢复原值
        eb = _acct_eb_ref[0]
        if eb is not None:
            try:
                if var_template.get():
                    if _acct_saved[0] is None:
                        _acct_saved[0] = "" if var_account.get() == _ACCT_PH else var_account.get()
                    var_account.set(_ACCT_PH)
                    # 用字体实际渲染宽度精确计算输入框宽度，避免估算偏大留白
                    try:
                        import tkinter.font as _tkfont
                        _f = _tkfont.Font(font=F)
                        _ph_w = int(_f.measure(_ACCT_PH) / _f.measure("0")) + 1
                    except Exception:
                        _ph_w = sum(2 if ord(c) > 0x2E80 else 1 for c in _ACCT_PH) + 2
                    eb.config(width=_ph_w, state="readonly", bg=DEEP, fg="#EF4444",
                              readonlybackground=DEEP,
                              insertbackground="#EF4444", highlightbackground="#334155")
                else:
                    var_account.set(_acct_saved[0] or "")
                    _acct_saved[0] = None
                    eb.config(width=11, state="normal", bg=DEEP, fg="#F1F5F9",
                              insertbackground="#F1F5F9", highlightbackground="#334155")
            except Exception:
                pass

    var_template.trace_add("write", _template_changed)

    # 「不在原路径生成」依赖「同步转换到游戏相册」：复制未勾选时强制关闭（灰）且不可勾选
    def _copy_trace(*_):
        cb = _no_local_cb_ref[0]
        if cb is not None:
            try:
                if var_copy_game.get():
                    cb.config(state="normal")
                else:
                    var_no_local.set(False)
                    cb.config(state="disabled")
            except Exception:
                pass

    var_copy_game.trace_add("write", _copy_trace)

    def _entry(parent, var, w=16, justify="left"):
        e = tk.Entry(parent, textvariable=var, width=w, bg=DEEP, fg="#F1F5F9",
                     insertbackground="#F1F5F9", relief="flat", font=F,
                     justify=justify,
                     disabledbackground="#334155", disabledforeground="#94A3B8",
                     readonlybackground=DEEP)
        e.configure(highlightthickness=1, highlightbackground="#334155", highlightcolor=ACC)
        return e

    # ---- 日志卡片（say 依赖 log，先建） ----
    lcard = tk.Frame(main_frame, bg=CARD, padx=16, pady=10,
                     highlightthickness=1, highlightbackground=BORDER)
    lcard.grid(row=4, column=0, columnspan=5, padx=18, pady=(12, 0), sticky="nsew")
    tk.Label(lcard, text="运行日志", bg=CARD, fg=TXT, font=F_B
             ).grid(row=0, column=0, sticky="w", pady=(0, 4))
    log = tk.Text(lcard, height=7, state="disabled", font=("Consolas", 9),
                  bg=DEEP, fg="#7DD3FC", insertbackground="#E2E8F0",
                  relief="flat", padx=8, pady=6, wrap="word")
    log.grid(row=1, column=0, sticky="nsew")
    sb = SlimScrollbar(lcard, command=log.yview, width=16)
    sb.grid(row=1, column=1, sticky="ns")
    log.configure(yscrollcommand=sb.set)
    lcard.rowconfigure(1, weight=1)
    lcard.columnconfigure(0, weight=1)

    def say(msg):
        log.configure(state="normal")
        log.insert("end", time.strftime("[%H:%M:%S] ") + msg + "\n")
        log.see("end")
        log.configure(state="disabled")

    def pick_files(title, exts, initialdir=None):
        files = filedialog.askopenfilenames(title=title, filetypes=exts, initialdir=initialdir or os.path.expanduser("~"))
        return list(files)

    # 各功能区独立记忆的"上次目录"（跨重启保存到 settings.json）
    _last_dirs = {}

    def _game_album_path():
        """游戏相册固定路径（写死，不随输入框修改）"""
        return os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                            "NGR", "Saved", "GameAlbum")

    def _game_dir_init():
        """游戏相册目录（跟随输入框）：存在则用它，不存在则回退到用户主目录（避免文件对话框乱跳）"""
        d = var_game_dir.get().strip()
        if d and os.path.isdir(d):
            return d
        return os.path.expanduser("~")

    def outdir_for(paths):
        if not paths:
            return None
        d = os.path.join(os.path.dirname(paths[0]), "转换输出")
        os.makedirs(d, exist_ok=True)
        return d

    def do_apd2jpg():
        fs = pick_files("选择 APD 文件", [("GameAlbum 截图", "*.APD"), ("所有文件", "*.*")],
                        initialdir=_last_dirs.get("apd2jpg") or _game_album_path())
        if not fs: return
        _last_dirs["apd2jpg"] = os.path.dirname(fs[0]); _save_settings()
        _run_apd2jpg(fs)

    def _run_apd2jpg(fs):
        od = outdir_for(fs)
        n_ok = n_fail = 0
        for f in fs:
            try:
                outs, fields = apd_to_images(f, od, export_png=var_png.get(), export_tail=var_tail_out.get())
                n_ok += 1
                say("APD→JPG  {}  →  {}  (头部: {})".format(
                    os.path.basename(f), ", ".join(os.path.basename(x) for x in outs),
                    " | ".join(str(x) for x in fields if x)))
            except Exception as ex:
                n_fail += 1
                say("失败 {}: {}".format(os.path.basename(f), ex))
        if n_fail:
            messagebox.showwarning("完成（有失败）", "成功 {} 个，失败 {} 个（详见下方日志）。\n输出目录：{}".format(n_ok, n_fail, od))
        else:
            messagebox.showinfo("完成", "转换完成，共 {} 个。输出目录：\n{}".format(n_ok, od))

    def do_apd2png():
        fs = pick_files("选择 APD 文件", [("GameAlbum 截图", "*.APD"), ("所有文件", "*.*")],
                        initialdir=_last_dirs.get("apd2png") or _game_album_path())
        if not fs: return
        _last_dirs["apd2png"] = os.path.dirname(fs[0]); _save_settings()
        _run_apd2png(fs)

    def _run_apd2png(fs):
        od = outdir_for(fs)
        n_ok = n_fail = 0
        for f in fs:
            try:
                outs, fields = apd_to_png(f, od, export_jpg=var_jpg.get(), export_tail=var_tail_out.get())
                n_ok += 1
                say("APD→PNG  {}  →  {}".format(
                    os.path.basename(f), ", ".join(os.path.basename(x) for x in outs)))
            except Exception as ex:
                n_fail += 1
                say("失败 {}: {}".format(os.path.basename(f), ex))
        if n_fail:
            messagebox.showwarning("完成（有失败）", "成功 {} 个，失败 {} 个（详见下方日志）。\n输出目录：{}".format(n_ok, n_fail, od))
        else:
            messagebox.showinfo("完成", "转换完成，共 {} 个。输出目录：\n{}".format(n_ok, od))

    def do_apt2png():
        fs = pick_files("选择 APT 文件", [("GameAlbum 缩略图", "*.APT"), ("所有文件", "*.*")],
                        initialdir=_last_dirs.get("apt2png"))
        if not fs: return
        _last_dirs["apt2png"] = os.path.dirname(fs[0]); _save_settings()
        _run_apt2png(fs)

    def _run_apt2png(fs):
        od = outdir_for(fs)
        n_ok = n_fail = 0
        for f in fs:
            try:
                out = apt_to_png(f, od)
                n_ok += 1
                say("APT→PNG  {}  →  {}".format(os.path.basename(f), os.path.basename(out)))
            except Exception as ex:
                n_fail += 1
                say("失败 {}: {}".format(os.path.basename(f), ex))
        if n_fail:
            messagebox.showwarning("完成（有失败）", "成功 {} 个，失败 {} 个（详见下方日志）。\n输出目录：{}".format(n_ok, n_fail, od))
        else:
            messagebox.showinfo("完成", "转换完成，共 {} 个。输出目录：\n{}".format(n_ok, od))

    def do_img2apd():
        fs = pick_files("选择图片（JPG/PNG）", [("图片", "*.jpg *.jpeg *.png *.bmp"), ("所有文件", "*.*")],
                        initialdir=_last_dirs.get("img2apd"))
        if not fs: return
        _last_dirs["img2apd"] = os.path.dirname(fs[0]); _save_settings()
        _run_img2apd(fs)

    def _run_img2apd(fs):
        """图片→APD 核心转换（选择文件与拖放共用）"""
        tpl = var_template_path.get().strip() if var_template.get() else ""
        if var_template.get() and (not tpl or not os.path.isfile(tpl)):
            if not tpl:
                messagebox.showwarning("需要模板", "已勾选「使用游戏原生截图 APD 作模板」，请先点击「选择模板…」选一张游戏原生截图。")
            else:
                messagebox.showwarning("模板无效", "模板文件不存在或路径无效：\n{}\n请重新选择。".format(tpl))
            return
        # 勾选「不在原路径生成」时：转换产物只复制到游戏相册，原图片目录不留文件
        no_local = var_no_local.get() and var_copy_game.get()
        tmp_dir = None
        if no_local:
            import tempfile
            tmp_dir = tempfile.mkdtemp(prefix="galb_")
            od = tmp_dir
        else:
            od = outdir_for(fs)
        # 转换前确认提示（选「否」则取消本次转换）：汇总所有可能导致游戏无法识别的勾选情况
        warns = []
        if var_copy_game.get():
            gd = var_game_dir.get().strip()
            if not os.path.isdir(gd):
                warns.append("「游戏相册目录」路径不存在：\n{}\n\n"
                             "若继续，程序会自动创建该目录；如果路径填错了，文件会被复制到错误位置。".format(gd or "(空)"))
        if not var_template.get() and not (var_account.get() or "").strip() and var_copy_game.get():
            warns.append("尚未填写账号 ID，且已勾选「同步转换到游戏相册」。\n"
                         "未填写 ID 生成的文件名将以 0 开头（如 0_时间戳.APD），复制进游戏相册后可能无法正常识别。\n"
                         "（使用模板打包时会自动读取模板的账号 ID，无需填写。）")
        if var_keep_name.get() and var_copy_game.get():
            warns.append("已勾选「使用原文件名」，且已勾选「同步转换到游戏相册」。\n"
                         "使用原图片名命名时，游戏相册对文件名有识别规则，复制进游戏相册后可能无法正常识别。\n"
                         "（默认的 账号ID_时间戳 命名可正常识别，建议先在游戏内实测。）")
        if warns:
            if not messagebox.askyesno("转换前确认",
                                       "\n\n".join(warns) + "\n\n是否仍要继续转换并复制？"):
                return
        game_dir = var_game_dir.get().strip() if var_copy_game.get() else None
        n_ok = n_fail = n_copy = 0
        for f in fs:
            try:
                fname = None
                if var_keep_name.get():
                    fname = os.path.splitext(os.path.basename(f))[0]
                if tpl:
                    apd, apt = make_apd_from_template(tpl, f, od, filename=fname,
                                                       make_apt=var_apt.get(), add_tail=False,
                                                       quality=_quality(var_quality5))
                    say("图片→APD(模板)  {}  →  {}{}".format(
                        os.path.basename(f), os.path.basename(apd),
                        "  + " + os.path.basename(apt) if apt else ""))
                else:
                    apd, apt = image_to_apd(f, od, filename=fname, account=var_account.get(), role="", map_name="",
                                            icon="LandmarkIcon",
                                            make_apt=var_apt.get(), add_tail=False,
                                            quality=_quality(var_quality5))
                    say("图片→APD  {}  →  {}{}".format(os.path.basename(f), os.path.basename(apd),
                                                       "  + " + os.path.basename(apt) if apt else ""))
                n_ok += 1
                if game_dir:
                    os.makedirs(game_dir, exist_ok=True)
                    bd = os.path.join(game_dir, "复制备份_" + time.strftime("%Y%m%d_%H%M%S"))
                    for src, dst in [(apd, os.path.join(game_dir, os.path.basename(apd))),
                                     (apt, os.path.join(game_dir, os.path.basename(apt)) if apt else None)]:
                        if dst is None: continue
                        if os.path.exists(dst):
                            os.makedirs(bd, exist_ok=True)
                            shutil.copy2(dst, os.path.join(bd, os.path.basename(dst)))
                        shutil.copy2(src, dst)
                    n_copy += 1
                    say("已复制到游戏相册目录：{}".format(game_dir))
            except Exception as ex:
                n_fail += 1
                say("失败 {}: {}".format(os.path.basename(f), ex))
        if tmp_dir:
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)   # 临时目录中的文件已全部复制走，清理掉
            except Exception:
                pass
        show_od = (game_dir or od) if no_local else od
        if n_fail:
            messagebox.showwarning("完成（有失败）", "成功 {} 个，失败 {} 个（详见下方日志）。\n输出目录：{}".format(n_ok, n_fail, show_od))
        else:
            msg = "转换完成，共 {} 个。输出目录：\n{}".format(n_ok, show_od)
            if n_copy:
                msg += "\n\n已复制 {} 个到游戏相册目录：\n{}".format(n_copy, game_dir)
                msg += "\n（游戏目录中的同名旧文件已自动备份到「复制备份_时间戳」文件夹）"
            messagebox.showinfo("完成", msg)

    def do_png2apt():
        fs = pick_files("选择 PNG 缩略图", [("PNG 图片", "*.png"), ("所有文件", "*.*")],
                        initialdir=_last_dirs.get("png2apt"))
        if not fs: return
        _last_dirs["png2apt"] = os.path.dirname(fs[0]); _save_settings()
        _run_png2apt(fs)

    def _run_png2apt(fs):
        od = outdir_for(fs)
        n_ok = n_fail = 0
        for f in fs:
            try:
                out = png_to_apt(f, od)
                n_ok += 1
                say("PNG→APT  {}  →  {}".format(os.path.basename(f), os.path.basename(out)))
            except Exception as ex:
                n_fail += 1
                say("失败 {}: {}".format(os.path.basename(f), ex))
        if n_fail:
            messagebox.showwarning("完成（有失败）", "成功 {} 个，失败 {} 个（详见下方日志）。\n输出目录：{}".format(n_ok, n_fail, od))
        else:
            messagebox.showinfo("完成", "转换完成，共 {} 个。输出目录：\n{}".format(n_ok, od))

    def _backup_originals(paths):
        """把原 APD 及同名 APT 复制到固定备份文件夹「替换备份」（多次操作合并存放，同名自动加时间戳）"""
        d = os.path.join(os.path.dirname(paths[0]), "替换备份")
        os.makedirs(d, exist_ok=True)
        for p in paths:
            for src in (p, os.path.splitext(p)[0] + ".APT"):
                if os.path.exists(src):
                    dst = os.path.join(d, os.path.basename(src))
                    if os.path.exists(dst):
                        root, ext = os.path.splitext(dst)
                        dst = "{}_{}{}".format(root, time.strftime("%H%M%S"), ext)
                    shutil.copy2(src, dst)
        return d

    def do_replace():
        # 第一步：强制定位到游戏相册目录（不跟随记忆）
        apds = pick_files("第一步：选择要替换图片的 APD 文件（注意：选 .APD，不要选 .APT）",
                          [("GameAlbum 截图", "*.APD"), ("所有文件", "*.*")],
                          initialdir=_game_album_path())
        if not apds: return
        # 第二步：独立记忆上次选择的目录
        imgs = pick_files("第二步：选择新图片（JPG/PNG，多选时按顺序对应）",
                          [("图片", "*.jpg *.jpeg *.png *.bmp"), ("所有文件", "*.*")],
                          initialdir=_last_dirs.get("replace_img") or os.path.expanduser("~"))
        if not imgs: return
        _last_dirs["replace_img"] = os.path.dirname(imgs[0]); _save_settings()
        keep_bak = var_backup.get()
        if not messagebox.askyesno("确认替换",
                                   ("将把原 APD 及同名 APT 复制备份到「替换备份」文件夹（多次操作的备份合并存放，同名自动加时间戳），\n"
                                    "然后在原位置直接替换为新图片（同名 APT 是否同步替换以勾选为准），是否继续？"
                                    if keep_bak else
                                    "「保留备份」已关闭：将直接覆盖原文件且不留任何备份，\n"
                                    "然后在原位置直接替换为新图片（同名 APT 是否同步替换以勾选为准），是否继续？")):
            return
        backup_dir = _backup_originals(apds) if keep_bak else None
        n_ok = n_fail = 0
        for i, a in enumerate(apds):
            new_img = imgs[i % len(imgs)]
            try:
                # 输出到原文件所在目录（原名同名），即"原地替换"
                apd, apt, fields = replace_apd_image(a, new_img, os.path.dirname(a),
                                                     update_tail=False,
                                                     update_apt=var_apt_upd.get(),
                                                     quality=_quality(var_quality6))
                n_ok += 1
                keep = " | ".join(x for x in fields if x and "分享码" not in x)
                code = next((x for x in fields if x and "分享码" in x), "")
                say("替换  {}  →  已原地替换{}".format(os.path.basename(a), "，原文件已备份" if keep_bak else ""))
                try:
                    from PIL import Image
                    import io as _io
                    _r = open(apd, "rb").read()
                    _pos = _r.find(ENC_JPEG_HEAD)
                    _jl = struct.unpack("<I", _r[_pos - 4:_pos])[0]
                    _im = Image.open(_io.BytesIO(xor_xform(_r[_pos:_pos + _jl])))
                    say("       主图分辨率：{} × {}  （源图：{}）".format(
                        _im.size[0], _im.size[1], os.path.basename(new_img)))
                except Exception:
                    pass
                say("       保留: {} {}".format(keep, code))
            except Exception as ex:
                n_fail += 1
                say("失败 {}: {}".format(os.path.basename(a), ex))
        if n_fail:
            messagebox.showwarning("完成（有失败）",
                                   "成功 {} 个，失败 {} 个（详见下方日志）。\n{}".format(
                                       n_ok, n_fail, ("原文件备份目录：{}".format(backup_dir)) if keep_bak else "未保留备份"))
        else:
            messagebox.showinfo("完成",
                                "替换完成，共 {} 个（已在原位置替换成功，文件名未变）。\n{}".format(
                                    n_ok, ("原文件备份目录：\n{}".format(backup_dir)) if keep_bak else "未保留备份"))

    # ---- 功能选择卡片（现代化卡片按钮） ----
    bcard = tk.Frame(main_frame, bg=CARD, padx=16, pady=12)
    bcard.grid(row=2, column=0, columnspan=5, padx=18, pady=(12, 0), sticky="ew")
    tk.Label(bcard, text="选择一个功能", bg=CARD, fg=TXT, font=F_B
             ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

    feats = [
        ("APD → JPG", "提取主图（可同时输出 PNG）", "apd2jpg"),
        ("APD → PNG", "提取主图（可同时输出 JPG）", "apd2png"),
        ("PNG → APT", "把 PNG 打包成缩略图", "png2apt"),
        ("APT → PNG", "解出缩略图", "apt2png"),
        ("图片 → APD（推荐）", "把 JPG/PNG 打包成游戏格式", "img2apd"),
        ("替换 APD 图片", "保留原信息/文件名，只换图", "replace"),
    ]
    sel = {"kid": None}

    def _auto_wrap(w, pad=10):
        """说明文字随面板宽度自动换行（最小 240px，窗口过窄时也不会逐字竖排）"""
        def _c(e):
            try:
                w.configure(wraplength=max(240, e.width - pad))
            except Exception:
                pass
        w.bind("<Configure>", _c)
    cards = {}

    # ---- 悬停提示 (ToolTip)：鼠标放上去延迟显示说明 ----
    class ToolTip:
        def __init__(self, widget, text, fg=None, wraplength=620):
            self.widget = widget
            self.text = text
            self.fg = fg or TXT
            self._wrap = wraplength
            self._tip = None
            self._after = None
            widget.bind("<Enter>", self._schedule, add="+")
            widget.bind("<Leave>", self._hide, add="+")

        def _schedule(self, _e=None):
            self._cancel()
            self._after = self.widget.after(300, self._show)

        def _show(self):
            if self._tip is not None:
                return
            x = self.widget.winfo_rootx() + 10
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
            self._tip = tk.Toplevel(self.widget)
            self._tip.wm_overrideredirect(True)
            self._tip.attributes("-topmost", True)
            self._tip.geometry("+{}+{}".format(x, y))
            lab = tk.Label(self._tip, text=self.text, justify="left",
                           bg=CARD2, fg=self.fg, font=("Microsoft YaHei UI", 12),
                           padx=12, pady=10, wraplength=self._wrap,
                           highlightthickness=1, highlightbackground="#334155")
            lab.pack()

        def _cancel(self):
            if self._after is not None:
                try:
                    self.widget.after_cancel(self._after)
                except Exception:
                    pass
                self._after = None

        def _hide(self, _e=None):
            self._cancel()
            if self._tip is not None:
                try:
                    self._tip.destroy()
                except Exception:
                    pass
                self._tip = None

    def _opt(row, text, var, tip, col=0, colspan=8, padx=2, pady=3):
        """复选框 + (?) 悬停提示，同一行排列"""
        fr = tk.Frame(opt_frame, bg=CARD)
        fr.grid(row=row, column=col, columnspan=colspan, sticky="w", padx=padx, pady=pady)
        cb = tk.Checkbutton(fr, text=text, variable=var, bg=CARD, fg=TXT, selectcolor=DEEP,
                            activebackground=CARD, activeforeground=TXT, font=F,
                            relief="flat", bd=0, highlightthickness=0)
        cb.pack(side="left")
        h = tk.Label(fr, text="(?)", bg=CARD, fg=ACC, font=("Microsoft YaHei UI", 12, "bold"),
                     cursor="question_arrow")
        h.pack(side="left", padx=(4, 0))
        ToolTip(h, tip)
        return cb

    def _pick_template():
        fs = pick_files("选择游戏原生 APD 模板（游戏内截图的相册）",
                        [("GameAlbum 截图", "*.APD"), ("所有文件", "*.*")],
                        initialdir=_game_album_path())
        if fs:
            var_template_path.set(fs[0])
            _last_dirs["img2apd"] = os.path.dirname(fs[0]); _save_settings()
            say("已选模板：" + os.path.basename(fs[0]))

    _current_kid = ["img2apd"]  # 当前选中的功能（拖放按此分发）

    def _on_drop(e):
        """拖放文件到参数区：按当前功能分发到对应 _run_xxx"""
        kid = _current_kid[0]
        # 各功能接受的扩展名
        ext_map = {
            "apd2jpg": (".apd",), "apd2png": (".apd",),
            "apt2png": (".apt",), "png2apt": (".png",),
            "img2apd": (".jpg", ".jpeg", ".png", ".bmp"),
        }
        want = ext_map.get(kid)
        if want is None:
            say("当前功能不支持拖放")
            return
        try:
            paths = root.tk.splitlist(e.data)
        except Exception:
            paths = str(e.data).split()
        good = [p for p in paths if os.path.isfile(p) and os.path.splitext(p)[1].lower() in want]
        bad = [p for p in paths if p not in good]
        if not good:
            say("拖放：格式不匹配，需要 {}".format("/".join(want)))
            messagebox.showwarning("无法转换",
                                   "当前功能需要 {} 文件。\n\n请拖入对应格式的文件。".format(" / ".join(want)))
            return
        if bad:
            say("已忽略 {} 个不匹配文件".format(len(bad)))
        say("拖放 {} 个文件，开始转换…".format(len(good)))
        if kid == "apd2jpg":
            _last_dirs["apd2jpg"] = os.path.dirname(good[0]); _save_settings()
            _run_apd2jpg(good)
        elif kid == "apd2png":
            _last_dirs["apd2png"] = os.path.dirname(good[0]); _save_settings()
            _run_apd2png(good)
        elif kid == "apt2png":
            _last_dirs["apt2png"] = os.path.dirname(good[0]); _save_settings()
            _run_apt2png(good)
        elif kid == "png2apt":
            _last_dirs["png2apt"] = os.path.dirname(good[0]); _save_settings()
            _run_png2apt(good)
        elif kid == "img2apd":
            _last_dirs["img2apd"] = os.path.dirname(good[0]); _save_settings()
            _run_img2apd(good)

    def _reg_drop(w):
        """把控件及其所有子控件都注册为拖放目标（整个参数面板都可拖入）"""
        try:
            w.drop_target_register(DND_FILES)
            w.dnd_bind("<<Drop>>", _on_drop)
        except Exception:
            pass
        for c in w.winfo_children():
            _reg_drop(c)

    def build_opts(kid):
        for w in opt_frame.winfo_children():
            w.destroy()
        _hdr = tk.Frame(opt_frame, bg=CARD)
        _hdr.grid(row=0, column=0, columnspan=8, sticky="w", pady=(0, 8))
        tk.Label(_hdr, text="▍", bg=CARD, fg=ACC, font=("Microsoft YaHei UI", 13, "bold")
                 ).pack(side="left")
        tk.Label(_hdr, text="参数设置（仅当前功能使用）", bg=CARD, fg=TXT, font=F_B
                 ).pack(side="left", padx=(4, 0))
        if kid == "apd2jpg":
            _opt(1, "同时输出 PNG 主图（默认只出 JPG）", var_png,
                 "解包 APD 时除 JPG 主图外，再额外输出一份 PNG 格式的主图。\n不勾则只输出 JPG。")
            _opt(2, "导出尾部缩略图（_thumb.png）", var_tail_out,
                 "额外解出 APD 内部尾部嵌着的小缩略图，保存为 _thumb.png。\n不勾则只解主图。\n如原文件无尾部缩略图则不会导出。")
            _w_hint = tk.Label(opt_frame, text="提示：转换后的文件输出在所选 APD 所在目录的 「转换输出」子文件夹中。",
                               bg=CARD, fg=SUB, font=("Microsoft YaHei UI", 11))
            _w_hint.grid(row=3, column=0, columnspan=8, sticky="w", padx=2, pady=(8, 0))
        elif kid == "apd2png":
            _opt(1, "同时输出 JPG 主图（默认只出 PNG）", var_jpg,
                 "解包 APD 时除 PNG 主图外，再额外输出一份 JPG 格式的主图。\n不勾则只输出 PNG。")
            _opt(2, "导出尾部缩略图（_thumb.png）", var_tail_out,
                 "额外解出 APD 内部尾部嵌着的小缩略图，保存为 _thumb.png。\n不勾则只解主图。\n如原文件无尾部缩略图则不会导出。")
            _w_hint = tk.Label(opt_frame, text="提示：转换后的文件输出在所选 APD 所在目录的 「转换输出」子文件夹中。",
                               bg=CARD, fg=SUB, font=("Microsoft YaHei UI", 11))
            _w_hint.grid(row=3, column=0, columnspan=8, sticky="w", padx=2, pady=(8, 0))
        elif kid == "img2apd":
            row1f = tk.Frame(opt_frame, bg=CARD)
            row1f.grid(row=1, column=0, columnspan=8, sticky="w", pady=3)
            fr_q = tk.Frame(row1f, bg=CARD)
            fr_q.pack(side="left", padx=(2, 16))
            tk.Label(fr_q, text="图片质量", bg=CARD, fg=TXT, font=F).pack(side="left")
            sc_q = tk.Spinbox(fr_q, from_=1, to=100, increment=5, textvariable=var_quality5,
                              width=6, justify="center", bg=DEEP, fg=TXT,
                              buttonbackground=CARD2, relief="flat",
                              highlightthickness=1, highlightbackground="#334155",
                              insertbackground="#F1F5F9",
                              validate="key", validatecommand=_q_vcmd,
                              font=F)
            sc_q.pack(side="left", padx=(4, 0))
            # 输入法组合输入会绕过 validate，这里在每次按键后兜底清理非法内容
            _q_last5 = [str(var_quality5.get())]
            def _q_guard5(*_):
                try:
                    _txt = sc_q.get()
                except Exception:
                    return
                if _txt != _q_last5[0]:
                    if _txt == "":
                        _q_last5[0] = _txt
                    elif not _txt.isdigit() or int(_txt) > 100:
                        try:
                            sc_q.delete(0, "end")
                            sc_q.insert(0, _q_last5[0])
                        except Exception:
                            pass
                    else:
                        _q_last5[0] = _txt
            sc_q.bind("<KeyRelease>", _q_guard5, add="+")
            def _q_focusout5(*_):
                if str(sc_q.get()).strip() == "":
                    var_quality5.set(100)
                    _q_last5[0] = "100"
            sc_q.bind("<FocusOut>", _q_focusout5, add="+")
            _q_tip = ("打包转 APD 时的压缩质量（1-100）。\n"
                      "默认 100：画质最高、文件较大；调低可缩小体积但画质下降。\n"
                      "JPG 源图不受影响（原样无损嵌入）。")
            h_q = tk.Label(fr_q, text="(?)", bg=CARD, fg=ACC, font=("Microsoft YaHei UI", 12, "bold"),
                           cursor="question_arrow")
            h_q.pack(side="left", padx=(4, 0))
            ToolTip(h_q, _q_tip)
            # 账号ID 整块（标签+输入框+问号）：勾选模板时整体隐藏
            _acct_row_ref = [None]
            fr_acct = tk.Frame(row1f, bg=CARD)
            fr_acct.pack(side="left", padx=(2, 6))
            _acct_row_ref[0] = fr_acct
            tk.Label(fr_acct, text="账号ID（留空=0）", bg=CARD, fg=TXT, font=F).pack(side="left")
            e_acct = _entry(fr_acct, var_account, 11, justify="center")
            e_acct.pack(side="left", padx=(2, 0))
            _acct_eb_ref[0] = e_acct
            _acct_tip = ("打包后 APD 文件名里的账号段（游戏左下角 ID）。\n"
                         "不勾模板时：留空自动写 0，填游戏账号 ID 可让相册归到该账号。\n"
                         "勾选「使用游戏原生截图 APD 作模板」时：以模板的账号为准，本框填写的值不生效。")
            h_acct = tk.Label(fr_acct, text="(?)", bg=CARD, fg=ACC, font=("Microsoft YaHei UI", 12, "bold"),
                              cursor="question_arrow")
            h_acct.pack(side="left", padx=(4, 0))
            ToolTip(h_acct, _acct_tip, wraplength=780)
            # 两个缩略图选项放进同一行弹性容器，紧凑排列（避免被列权重推到右侧）
            row2f = tk.Frame(opt_frame, bg=CARD)
            row2f.grid(row=2, column=0, columnspan=8, sticky="w", pady=3)
            fr_apt = tk.Frame(row2f, bg=CARD)
            fr_apt.pack(side="left")
            cb_apt = tk.Checkbutton(fr_apt, text="同时生成 APT 缩略图", variable=var_apt,
                                    bg=CARD, fg=TXT, selectcolor=DEEP, activebackground=CARD,
                                    activeforeground=TXT, font=F, relief="flat", bd=0, highlightthickness=0)
            cb_apt.pack(side="left")
            _apt_tip = "打包图片时顺带生成同名 .APT 缩略图文件，供游戏相册列表显示。\n不勾则只生成 APD。"
            h_apt = tk.Label(fr_apt, text="(?)", bg=CARD, fg=ACC, font=("Microsoft YaHei UI", 12, "bold"),
                             cursor="question_arrow")
            h_apt.pack(side="left", padx=(4, 0))
            ToolTip(h_apt, _apt_tip)
            fr_name = tk.Frame(row2f, bg=CARD)
            fr_name.pack(side="left", padx=(16, 0))
            cb_name = tk.Checkbutton(fr_name, text="使用原文件名", variable=var_keep_name,
                                     bg=CARD, fg="#EF4444", selectcolor=DEEP, activebackground=CARD,
                                     activeforeground="#EF4444", font=F, relief="flat", bd=0, highlightthickness=0)
            cb_name.pack(side="left")
            _name_tip = ("生成的 APD/APT 使用原图片的文件名（不含扩展名）命名，方便辨认。\n"
                         "默认关闭（默认用 账号ID_时间戳 命名）。\n"
                         "注意：游戏相册对文件名有识别规则，使用原文件名可能影响游戏内识别，请自行实测。")
            # 「使用原文件名」帮助文本红色警示，悬停文字即显示（其他选项保持只在 (?) 显示）
            ToolTip(cb_name, _name_tip, fg="#EF4444")
            h_name = tk.Label(fr_name, text="(?)", bg=CARD, fg="#EF4444",
                              font=("Microsoft YaHei UI", 12, "bold"),
                              cursor="question_arrow")
            h_name.pack(side="left", padx=(4, 0))
            ToolTip(h_name, _name_tip, fg="#EF4444")
            _tpl_cb_ref[0] = _opt(3, "使用游戏相册原生截图APD做模板", var_template,
                 "勾选后建议以一张游戏原生截图做模板，完整复用该模板的账号ID/真实分享码/昵称/地图/JSON, 无需额外输入账号ID，只替换内部图片。\n"
                 "不勾选此选项，输入账号ID后，游戏也能正常识别图片，但无游戏原生截图信息(真实分享码/昵称/地图/JSON)。",
                 col=0, colspan=4, padx=2, pady=3)
            # 模板路径行：仅勾选模板时显示；此时账号ID整块隐藏（以模板账号为准）
            _tpl_row_ref = [None]
            def _tpl_row_sync(*_):
                if var_template.get():
                    if _tpl_row_ref[0] is not None:
                        _tpl_row_ref[0].grid()
                    if _acct_row_ref[0] is not None:
                        _acct_row_ref[0].pack_forget()
                else:
                    if _tpl_row_ref[0] is not None:
                        _tpl_row_ref[0].grid_remove()
                    if _acct_row_ref[0] is not None:
                        _acct_row_ref[0].pack(side="left", padx=(2, 6))
            var_template.trace_add("write", _tpl_row_sync)
            _template_changed()
            row4f = tk.Frame(opt_frame, bg=CARD)
            row4f.grid(row=4, column=0, columnspan=8, sticky="we", pady=3)
            _tpl_row_ref[0] = row4f
            _tpl_row_sync()
            tk.Label(row4f, text="模板路径", bg=CARD, fg=TXT, font=F).pack(side="left", padx=(2, 6))
            h_tpl = tk.Label(row4f, text="(?)", bg=CARD, fg=ACC, font=("Microsoft YaHei UI", 12, "bold"),
                             cursor="question_arrow")
            h_tpl.pack(side="left", padx=(4, 0))
            _tpl_tip = ("模板即游戏相册里、由游戏内拍照生成的原生截图 APD 文件。\n"
                        "游戏相册路径：C:\\Users\\用户名\\AppData\\Local\\NGR\\Saved\\GameAlbum\n"
                        "（AppData 是隐藏文件夹：资源管理器 → 查看 → 显示 → 勾选「隐藏的项目」即可显示；\n"
                        "  找不到时也可点左上角「游戏相册」按钮直接打开。）")
            ToolTip(h_tpl, _tpl_tip, wraplength=780)
            _entry(row4f, var_template_path, 30).pack(side="left", fill="x", expand=True)
            tk.Button(row4f, text="选择模板…", width=10,
                      command=lambda: _pick_template(),
                      bg=CARD2, fg=TXT, activebackground=BTN, activeforeground="white",
                      relief="flat", bd=0, padx=8, font=F, cursor="hand2", highlightthickness=0
                      ).pack(side="left", padx=(4, 0))
            row5f = tk.Frame(opt_frame, bg=CARD)
            row5f.grid(row=5, column=0, columnspan=8, sticky="w", pady=3)
            fr_copy = tk.Frame(row5f, bg=CARD)
            fr_copy.pack(side="left")
            cb_copy = tk.Checkbutton(fr_copy, text="同步转换到游戏相册（需重启游戏生效）", variable=var_copy_game,
                                     bg=CARD, fg=TXT, selectcolor=DEEP, activebackground=CARD,
                                     activeforeground=TXT, font=F, relief="flat", bd=0, highlightthickness=0)
            cb_copy.pack(side="left")
            _copy_tip = ("打包完成后把生成的 APD/APT 自动复制到游戏相册目录（路径在下方输入框，可修改）。\n"
                         "同名旧文件自动备份到「复制备份_时间戳」文件夹。\n"
                         "注意：使用模板打包时无需填写账号ID（自动取模板的账号）。")
            h_copy = tk.Label(fr_copy, text="(?)", bg=CARD, fg=ACC, font=("Microsoft YaHei UI", 12, "bold"),
                              cursor="question_arrow")
            h_copy.pack(side="left", padx=(4, 0))
            ToolTip(h_copy, _copy_tip)
            _nl_ref = [None]
            fr_nl = tk.Frame(row5f, bg=CARD)
            fr_nl.pack(side="left", padx=(16, 0))
            _nl_ref[0] = fr_nl
            cb_nl = tk.Checkbutton(fr_nl, text="仅转换到游戏相册（不在原路径输出）", variable=var_no_local,
                                   bg=CARD, fg=TXT, selectcolor=DEEP, activebackground=CARD,
                                   activeforeground=TXT, font=F, relief="flat", bd=0, highlightthickness=0)
            cb_nl.pack(side="left")
            _no_local_cb_ref[0] = cb_nl
            _nl_tip = ("勾选后，转换出的 APD/APT 不会放在原图片目录的「转换输出」文件夹，直接只复制到游戏相册目录。\n"
                       "仅当「同步转换到游戏相册」勾选时可开启；未勾选复制时强制关闭（文件必须保留在原路径）。")
            h_nl = tk.Label(fr_nl, text="(?)", bg=CARD, fg=ACC, font=("Microsoft YaHei UI", 12, "bold"),
                            cursor="question_arrow")
            h_nl.pack(side="left", padx=(4, 0))
            ToolTip(h_nl, _nl_tip)
            _copy_trace()
            # 游戏相册目录行：仅勾选同步转换到游戏相册时显示
            _copy_row_ref = [None]
            def _copy_row_sync(*_):
                if var_copy_game.get():
                    if _copy_row_ref[0] is not None:
                        _copy_row_ref[0].grid()
                    if _nl_ref[0] is not None:
                        _nl_ref[0].pack(side="left", padx=(16, 0))
                else:
                    if _copy_row_ref[0] is not None:
                        _copy_row_ref[0].grid_remove()
                    if _nl_ref[0] is not None:
                        _nl_ref[0].pack_forget()
            var_copy_game.trace_add("write", _copy_row_sync)
            row6f = tk.Frame(opt_frame, bg=CARD)
            row6f.grid(row=6, column=0, columnspan=8, sticky="we", pady=3)
            _copy_row_ref[0] = row6f
            _copy_row_sync()
            tk.Label(row6f, text="游戏相册目录", bg=CARD, fg=TXT, font=F).pack(side="left", padx=(2, 6))
            h_dir = tk.Label(row6f, text="(?)", bg=CARD, fg=ACC, font=("Microsoft YaHei UI", 12, "bold"),
                             cursor="question_arrow")
            h_dir.pack(side="left", padx=(4, 0))
            _dir_tip = ("游戏相册目录：C:\\Users\\用户名\\AppData\\Local\\NGR\\Saved\\GameAlbum\n"
                        "（AppData 是隐藏文件夹：资源管理器 → 查看 → 显示 → 勾选「隐藏的项目」即可显示；\n"
                        "  找不到时也可点左上角「游戏相册」按钮直接打开。）")
            ToolTip(h_dir, _dir_tip, wraplength=780)
            _entry(row6f, var_game_dir, 30).pack(side="left", fill="x", expand=True)
            tk.Button(row6f, text="浏览目录…", width=10,
                      command=lambda: var_game_dir.set(
                          filedialog.askdirectory(initialdir=_game_album_path()) or var_game_dir.get()),
                      bg=CARD2, fg=TXT, activebackground=BTN, activeforeground="white",
                      relief="flat", bd=0, padx=8, font=F, cursor="hand2", highlightthickness=0
                      ).pack(side="left", padx=(4, 0))
        elif kid == "replace":
            rowqf = tk.Frame(opt_frame, bg=CARD)
            rowqf.grid(row=1, column=0, columnspan=8, sticky="w", pady=3)
            tk.Label(rowqf, text="图片质量", bg=CARD, fg=TXT, font=F).pack(side="left", padx=(2, 6))
            sc_q6 = tk.Spinbox(rowqf, from_=1, to=100, increment=5, textvariable=var_quality6,
                               width=6, justify="center", bg=DEEP, fg=TXT,
                               buttonbackground=CARD2, relief="flat",
                               highlightthickness=1, highlightbackground="#334155",
                               insertbackground="#F1F5F9",
                               validate="key", validatecommand=_q_vcmd,
                               font=F)
            sc_q6.pack(side="left")
            _q_last6 = [str(var_quality6.get())]
            def _q_guard6(*_):
                try:
                    _txt = sc_q6.get()
                except Exception:
                    return
                if _txt != _q_last6[0]:
                    if _txt == "":
                        _q_last6[0] = _txt
                    elif not _txt.isdigit() or int(_txt) > 100:
                        try:
                            sc_q6.delete(0, "end")
                            sc_q6.insert(0, _q_last6[0])
                        except Exception:
                            pass
                    else:
                        _q_last6[0] = _txt
            sc_q6.bind("<KeyRelease>", _q_guard6, add="+")
            def _q_focusout6(*_):
                if str(sc_q6.get()).strip() == "":
                    var_quality6.set(100)
                    _q_last6[0] = "100"
            sc_q6.bind("<FocusOut>", _q_focusout6, add="+")
            h_q = tk.Label(rowqf, text="(?)", bg=CARD, fg=ACC, font=("Microsoft YaHei UI", 12, "bold"),
                           cursor="question_arrow")
            h_q.pack(side="left", padx=(4, 0))
            _q_tip = ("打包转 APD 时的压缩质量（1-100）。\n"
                      "默认 100：画质最高、文件较大；调低可缩小体积但画质下降。\n"
                      "JPG 源图不受影响（原样无损嵌入）。")
            ToolTip(h_q, _q_tip)
            _opt(2, "同时替换同名 APT 缩略图文件", var_apt_upd,
                 "替换 APD 时，同名 .APT 文件也换成新缩略图（推荐勾选）。\n不勾则 APT 保持原样。")
            _opt(3, "备份原文件", var_backup,
                 "开启（默认）：替换前把原 APD 及同名 APT 备份到「替换备份」文件夹，可随时复制回原位恢复。\n"
                 "关闭：直接覆盖，不留任何备份，原文件无法找回。")
            _w_note = tk.Label(opt_frame, text="说明：先选要替换的 APD，再选新图片；替换在原位置直接覆盖，原文件自动备份到「替换备份」文件夹。",
                               bg=CARD, fg=SUB, font=("Microsoft YaHei UI", 11))
            _w_note.grid(row=4, column=0, columnspan=8, sticky="w", padx=2, pady=(2, 0))
        else:
            tk.Label(opt_frame, text="此功能无需参数，点击下方按钮直接开始。",
                     bg=CARD, fg=SUB, font=F).grid(row=1, column=0, columnspan=8, sticky="w", padx=2)
        # 开始转换按钮行；图片→APD 时右侧并列拖放区（不单独占行）
        row9f = tk.Frame(opt_frame, bg=CARD)
        row9f.grid(row=9, column=0, columnspan=8, pady=(12, 4), sticky="w")
        tk.Button(row9f, text=("开始替换" if kid == "replace" else "开始转换"),
                  command=lambda: _start(kid), bg=OK, fg="white",
                  activebackground=OK_H, activeforeground="white", relief="flat", bd=0,
                  padx=22, pady=6, font=F_B, cursor="hand2", highlightthickness=0
                  ).pack(side="left")
        if kid != "replace" and DND_FILES is not None:
            _current_kid[0] = kid
            # 各功能拖入提示文案
            drop_hint = {
                "apd2jpg": "⇩ 可直接拖入 .APD 文件自动转换（整个参数区域均可）",
                "apd2png": "⇩ 可直接拖入 .APD 文件自动转换（整个参数区域均可）",
                "apt2png": "⇩ 可直接拖入 .APT 文件自动转换（整个参数区域均可）",
                "png2apt": "⇩ 可直接拖入 .PNG 文件自动转换（整个参数区域均可）",
                "img2apd": "⇩ 可直接拖入图片自动转换（整个参数区域均可）",
            }.get(kid, "⇩ 可直接拖入文件自动转换（整个参数区域均可）")
            drop_lab = tk.Label(row9f, text=drop_hint, bg=CARD2,
                                fg=ACC, font=F, padx=12, pady=6,
                                highlightthickness=1, highlightbackground="#334155",
                                cursor="hand2")
            drop_lab.pack(side="left", padx=(12, 0))
            # 整个参数面板（含所有控件）都可作为拖放目标
            _reg_drop(opt_frame)
        opt_frame.grid()

        def _blur_click(ev):
            """点击不可输入的控件（文字/问号/空白）时让输入框失焦"""
            w = ev.widget
            if isinstance(w, (tk.Entry, tk.Spinbox, tk.Text)):
                return
            try:
                root.focus_set()
            except Exception:
                pass

        def _bind_blur(parent):
            try:
                parent.bind("<Button-1>", _blur_click, add="+")
            except Exception:
                pass
            for c in parent.winfo_children():
                _bind_blur(c)

        _bind_blur(opt_frame)

    def select_feat(kid):
        if sel["kid"] == kid:
            # 再点同一张卡片 → 收起参数面板（再点才展开）
            sel["kid"] = None
            for k in cards:
                _card_paint(k)
            opt_frame.grid_remove()
            # 参数面板收起后立即刷新滚动范围（顶部从 0 起，无空白富余）
            root.after(30, lambda: main_canvas.configure(
                scrollregion=(0, 0, main_frame.winfo_width(), main_frame.winfo_reqheight())))
            return
        sel["kid"] = kid
        for k in cards:
            _card_paint(k)
        build_opts(kid)

    def _card_paint(kid, hover=False):
        """统一重绘一张功能卡片（正常 / 悬停 / 选中三态）"""
        f, tl, dl, bar = cards[kid]
        on = (sel["kid"] == kid)
        if on:
            bg, brd = SEL_BG, ACC
        elif hover:
            bg, brd = "#20334F", BTN_H
        else:
            bg, brd = CARD2, BORDER
        f.configure(bg=bg, highlightbackground=brd)
        tl.configure(bg=bg, fg="#FFFFFF" if on else TXT)
        dl.configure(bg=bg, fg=ACC if on else SUB)
        bar.configure(bg=bg, fg=ACC)

    # 6 张卡片：3 行 × 2 列，grid 均分（uniform 保证两列等宽、sticky 拉伸占满）
    for i, (t, d, kid) in enumerate(feats):
        r, c = divmod(i, 2)
        f = tk.Frame(bcard, bg=CARD2, highlightthickness=1, highlightbackground=BORDER)
        f.grid(row=1 + r, column=c, padx=6, pady=5, sticky="ew")
        f.columnconfigure(1, weight=1)
        bar = tk.Label(f, text="▍", bg=CARD2, fg=ACC, font=("Microsoft YaHei UI", 17, "bold"),
                       cursor="hand2")
        bar.grid(row=0, column=0, sticky="w", padx=(12, 4), pady=(8, 0))
        tl = tk.Label(f, text=t, bg=CARD2, fg=TXT, font=("Microsoft YaHei UI", 14, "bold"),
                      cursor="hand2")
        tl.grid(row=0, column=1, sticky="w", padx=(0, 12), pady=(10, 0))
        dl = tk.Label(f, text=d, bg=CARD2, fg=SUB, font=("Microsoft YaHei UI", 11), cursor="hand2")
        dl.grid(row=1, column=0, columnspan=2, sticky="w", padx=(12, 12), pady=(0, 10))
        for w in (f, tl, dl, bar):
            w.bind("<Enter>", lambda _e, k=kid: _card_paint(k, True))
            w.bind("<Leave>", lambda _e, k=kid: _card_paint(k, False))
            w.bind("<Button-1>", lambda _e, k=kid: select_feat(k))
        cards[kid] = (f, tl, dl, bar)
    bcard.columnconfigure(0, weight=1, uniform="fc")
    bcard.columnconfigure(1, weight=1, uniform="fc")

    # ---- 使用说明（独立窗口，重排版：分段落 + 表格化，免责声明并入底部） ----
    help_win = {"w": None, "cv": None}

    def _hl(parent, text, size=12, color=None, pady=6, bold=False):
        tk.Label(parent, text=text, bg=BG, fg=color or ("#F8FAFC" if bold else "#CBD5E1"),
                 font=("Microsoft YaHei UI", size, "bold" if bold else "normal"),
                 justify="left", anchor="w", wraplength=1100).pack(fill="x", pady=(pady, 2))

    def _tbl(parent, header, rows, widths):
        DIV = "#3B4A6B"  # 分隔线色
        N = len(header)
        hf = tk.Frame(parent, bg=CARD)
        hf.pack(fill="x", pady=(6, 2))
        # 列结构：cell | vline | cell | vline | cell
        # 表头
        for ci, htxt in enumerate(header):
            tk.Label(hf, text=htxt, bg=CARD2, fg=ACC, font=("Microsoft YaHei UI", 12, "bold"),
                     padx=10, pady=10, anchor="center").grid(row=0, column=ci * 2, sticky="we")
        # 表头下粗线
        tk.Frame(hf, bg=DIV, height=2).grid(row=1, column=0, columnspan=N * 2 - 1, sticky="we")
        # 内容行
        for ri, row in enumerate(rows):
            r = 2 + ri * 2
            for ci, val in enumerate(row):
                # 第一列（序号圆圈）放大加粗，其他列保持 11
                _font = ("Microsoft YaHei UI", 14, "bold") if ci == 0 else ("Microsoft YaHei UI", 11)
                tk.Label(hf, text=val, bg=CARD, fg=ACC if ci == 0 else TXT, font=_font,
                         padx=10, pady=9, anchor="center" if ci == 0 else "w", justify="left",
                         wraplength=widths[ci] + 120).grid(row=r, column=ci * 2, sticky="we")
            tk.Frame(hf, bg=DIV, height=1).grid(row=r + 1, column=0, columnspan=N * 2 - 1, sticky="we")
        # 竖线列
        total_rows = 2 + len(rows) * 2
        for ci in range(N - 1):
            tk.Frame(hf, bg=DIV, width=1).grid(row=0, column=ci * 2 + 1, rowspan=total_rows, sticky="ns")
        for ci in range(N):
            hf.columnconfigure(ci * 2, weight=1)

    def toggle_help():
        if help_win["w"] is not None and help_win["w"].winfo_exists():
            help_win["w"].lift()
            help_win["w"].focus_force()
            return
        win = tk.Toplevel(root)
        win.title("使用说明")
        win.configure(bg=BG)
        # 优先用上次记忆的窗口大小；没有则按 DPI 保持视觉大小恒定
        if _help_geo_saved[0]:
            try:
                win.geometry(_help_geo_saved[0])
            except Exception:
                _help_geo_saved[0] = None
        if not _help_geo_saved[0]:
            try:
                _sw, _sh = root.winfo_screenwidth(), root.winfo_screenheight()
                _w = int(880 * scale)
                _h = int(720 * scale)
                if _w > _sw: _w = int(_sw * 0.94)
                if _h > _sh: _h = int(_sh * 0.92)
                win.geometry("{}x{}".format(_w, _h))
                win.minsize(int(680 * scale), int(500 * scale))
            except Exception:
                win.geometry("1100x920")
                win.minsize(760, 560)
        help_win["w"] = win
        tk.Label(win, text="使用说明", bg=BG, fg="#F8FAFC",
                 font=("Microsoft YaHei UI", 17, "bold")).pack(anchor="w", padx=18, pady=(14, 6))
        body = tk.Frame(win, bg=BG)
        body.pack(fill="both", expand=True, padx=18, pady=(0, 8))
        cv = tk.Canvas(body, bg=BG, highlightthickness=0)
        vbar = SlimScrollbar(body, command=cv.yview)
        cv.configure(yscrollcommand=vbar.set)
        cv.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")
        help_win["cv"] = cv
        inner = tk.Frame(cv, bg=BG)
        cv.create_window((0, 0), window=inner, anchor="nw")
        help_win["inner"] = inner

        _hl(inner, "《王者荣耀世界》（NGR）游戏相册 GameAlbum 目录下 APD / APT 私有格式图片的转换与替换工具。\n"
                   "游戏相册内的截图是私有格式（APD/APT），无法直接用图片查看器打开。本工具基于对游戏格式的逆向分析，"
                   "实现 APD/APT 与常见图片格式（PNG/JPG）的双向转换，并支持无损替换游戏相册内原有 APD 的图片内容"
                   "（头部字段、分享码、结构保持不变），让游戏能够正常识别。",
            11, "#CBD5E1", 6, False)

        _hl(inner, "【游戏相册位置】", 13, "#F8FAFC", 10, True)
        _hl(inner, "游戏相册目录默认在：C:\\Users\\用户名\\AppData\\Local\\NGR\\Saved\\GameAlbum"
                   "（用户名 = 你电脑的登录用户名）。游戏在本地自动生成，游戏里拍的截图 / 录制的相册都存放在这里。")
        _hl(inner, "注意：AppData 是 Windows 隐藏文件夹，若找不到：文件资源管理器 → 查看 → 显示 → 勾选「隐藏的项目」即可显示；"
                   "也可用本工具左上角「游戏相册」按钮直接打开。如需改路径，可在「图片 → APD」参数里用「浏览目录…」按钮修改。",
            10, "#94A3B8")

        _hl(inner, "【文件格式说明】", 13, "#F8FAFC", 14, True)
        _tbl(inner, ["格式", "是什么", "说明"],
             [
                ["APD", "游戏相册主文件",
                 "记录截图信息（账号/昵称/地图/分享码）+ 加密的全尺寸大图（JPEG 格式）+ 尾部缩略图；用 57 C4 3A 21 循环异或加密，双击显示乱码是正常的，用本工具解包即可。"],
                ["APT", "相册缩略图文件",
                 "相册列表里显示的小图，与 APD 同名成对出现，可单独解包或打包。"],
                ["JPG", "有损压缩图片", "文件小、加载快，适合查看与分享。"],
                ["PNG", "无损压缩图片", "画质完全保留、文件较大，适合留档与二次编辑。"],
             ], [140, 250, 440])

        _hl(inner, "【六大功能一览】", 13, "#F8FAFC", 14, True)
        _tbl(inner, ["功能", "做什么", "使用要点"],
             [
                ["① APD → JPG", "提取主图，输出 JPG", "可选同时输出 PNG 主图、导出尾部缩略图（_thumb.png）。"],
                ["② APD → PNG", "提取主图，输出 PNG 无损格式", "可选同时输出 JPG 主图、导出尾部缩略图。"],
                ["③ PNG → APT", "把 PNG 打包成缩略图文件", "自动缩放到 400×225（游戏列表小图的标准尺寸）。"],
                ["④ APT → PNG", "把缩略图文件解成 PNG", "原样输出，不缩放。"],
                ["⑤ 图片 → APD", "把 JPG/PNG 打包成游戏相册文件",
                 "账号ID：相册归属账号（留空=0）；建议勾选「使用游戏原生截图 APD 作模板」复现真实分享码/昵称/地图；可选同时生成 APT 缩略图、同步转换到游戏相册。"],
                ["⑥ 替换 APD 图片", "只换图，保留全部原有信息",
                 "先选要替换的 APD（不选 APT）→ 再选新图片 → 原位覆盖；原文件自动备份到「替换备份」文件夹。"],
             ], [140, 250, 440])

        _hl(inner, "【选项说明】", 13, "#F8FAFC", 14, True)
        _tbl(inner, ["所属", "选项", "作用"],
             [
                ["①②", "同时输出 PNG/JPG 主图", "① 解出的图额外存一份 PNG 无损版；② 同理额外输出一份 JPG。"],
                ["①②", "导出尾部缩略图（_thumb.png）", "把 APD 尾部内嵌的小图导出为独立 PNG；如原文件无尾部缩略图则不会导出。"],
                ["⑤⑥", "图片质量", "打包转 APD 的压缩质量（1-100，默认 100 画质最高）；JPG 源图不受影响（原样无损嵌入）。"],
                ["⑤", "同时生成 APT 缩略图", "打包 APD 时顺带生成同名 .APT 文件，供相册列表显示。"],
                ["⑤", "使用原文件名", "生成的 APD/APT 用原图片文件名命名（默认用 账号ID_时间戳）；注意可能影响游戏内识别，请自行实测。"],
                ["⑤", "使用游戏原生截图 APD 作模板", "复用游戏原生截图的头部信息（真实分享码/昵称/账号/地图/JSON），只替换图片；推荐勾选。"],
                ["⑤", "同步转换到游戏相册", "打包完成后自动复制 APD/APT 到游戏相册目录；同名旧文件自动备份到「复制备份_时间戳」文件夹。"],
                ["⑤", "仅转换到游戏相册（不在原路径输出）", "文件不放在原图片目录的「转换输出」，只复制到游戏相册；需先勾选同步转换。"],
                ["⑥", "同时替换同名 APT 缩略图文件", "替换 APD 时同名 .APT 也换成新缩略图（默认勾选）。"],
                ["⑥", "备份原文件", "替换前把原文件备份到「替换备份」文件夹（默认开启），可随时恢复。"],
             ], [140, 250, 440])

        _hl(inner, "【通用说明】", 13, "#F8FAFC", 14, True)
        _hl(inner, "· 输出默认在源文件所在目录的「转换输出」子文件夹（替换功能例外：原位覆盖 + 自动备份）。\n"
                   "· 所有勾选、账号ID、路径自动保存（程序同目录「游戏相册转换工具_settings.json」），重启后自动恢复。\n"
                   "· 各功能区的「开始转换」文件选择框独立记忆各自的目录；「APD→JPG」「APD→PNG」和「替换第一步」默认定位到游戏相册目录。\n"
                   "· 主界面右下角「恢复默认」按钮：一键清空所有设置与目录记忆。\n"
                   "· 打包/替换后的文件结构与原版一致，但游戏能否识别需进游戏实测；游戏更新可能导致格式/密钥变化，工具可能失效，使用前请备份文件。\n"
                   "· 「替换备份」文件夹里的文件是操作前的原始文件，可随时复制回原位恢复。")

        _hl(inner, "【免责声明】", 13, "#EF4444", 14, True)
        dis_f = tk.Frame(inner, bg="#7F1D1D", padx=14, pady=10)
        dis_f.pack(fill="x", pady=(2, 6))
        tk.Label(dis_f, text=DISCLAIMER_TEXT, bg="#7F1D1D", fg="#FECACA",
                 font=("Microsoft YaHei UI", 11), justify="left", anchor="w",
                 wraplength=1100).pack(fill="x")

        inner.update_idletasks()
        cv.configure(scrollregion=(0, 0, inner.winfo_reqwidth(), inner.winfo_reqheight()))

        _close_help = lambda: (_help_geo_saved.__setitem__(0, win.geometry()),
                               help_win.__setitem__("w", None),
                               help_win.__setitem__("cv", None),
                               _save_settings(),
                               win.destroy())
        tk.Button(win, text="关闭", command=_close_help, bg=BTN, fg="white",
                  activebackground=BTN_H, activeforeground="white", relief="flat",
                  bd=0, padx=26, pady=6, font=F_B, cursor="hand2", highlightthickness=0
                  ).pack(anchor="e", padx=18, pady=(0, 14))
        win.protocol("WM_DELETE_WINDOW", _close_help)

    # ---- 参数面板（选中功能后在原窗口内展开，位于运行日志上方） ----
    opt_frame = tk.Frame(main_frame, bg=CARD, padx=16, pady=12)
    opt_frame.grid(row=3, column=0, columnspan=5, padx=18, pady=(10, 0), sticky="ew")
    opt_frame.grid_columnconfigure(0, weight=1)  # 第0列可伸缩，路径输入行（sticky="we"）能填满整行
    opt_frame.grid_remove()
    start_map = {"apd2jpg": do_apd2jpg, "img2apd": do_img2apd, "apt2png": do_apt2png,
                 "png2apt": do_png2apt, "replace": do_replace, "apd2png": do_apd2png}

    def _start(kid):
        opt_frame.grid()
        root.after(30, lambda: main_canvas.configure(
            scrollregion=(0, 0, main_frame.winfo_width(), main_frame.winfo_reqheight())))
        start_map[kid]()

    # ---- 底部固定栏（不随内容滚动，始终可见） ----
    bottom_bar = tk.Frame(root, bg=BG)
    bottom_bar.grid(row=1, column=0, columnspan=2, sticky="ew")

    # 底部右侧按钮组：恢复默认 + 使用说明（先占右侧，窄窗口也不被挤走）
    btn_bar = tk.Frame(bottom_bar, bg=BG)
    btn_bar.pack(side="right", padx=(0, 20), pady=(10, 12))

    # 左侧提示文字：单行自然排布（窗口过窄时会被裁掉一部分，不再自动换行占多行）
    hint_label = tk.Label(bottom_bar, text="提示：APD 为游戏私有格式，还原出的图片可直接双击查看；打包生成的 APD 与游戏结构、加密一致，"
                                           "能否被游戏识别需进游戏实测。\n输出默认在源文件目录的 「转换输出」子文件夹；"
                                           "游戏更新可能导致格式/密钥变化，工具可能随之失效，使用前请备份文件。",
                          bg=BG, fg="#64748B", font=("Microsoft YaHei UI", 10), justify="left")
    hint_label.pack(side="left", padx=20, pady=(10, 12))

    def reset_defaults():
        """一键恢复全部默认设置（删除配置文件）"""
        if not messagebox.askyesno("恢复默认",
                                   "确定恢复全部默认设置吗？\n所有勾选、账号ID、路径都会被清空为默认值。"):
            return
        try:
            os.remove(_SETTINGS_FILE)
        except Exception:
            pass
        _DEFAULTS = {
            "var_account": "", "var_apt": True, "var_tail": False,
            "var_tail_upd": False, "var_png": False, "var_jpg": False, "var_tail_out": False,
            "var_apt_upd": True, "var_backup": True, "var_copy_game": True, "var_game_dir": os.path.join(
                os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "NGR", "Saved", "GameAlbum"),
            "var_template": False, "var_template_path": "", "var_keep_name": False,
            "var_no_local": False, "var_quality5": 100, "var_quality6": 100,
        }
        for n, v in _SETTINGS_VARS:
            try:
                v.set(_DEFAULTS.get(n, v.get()))
            except Exception:
                pass
        _last_dirs.clear()  # 各功能区的目录记忆一并清空
        _save_settings()
        say("已恢复全部默认设置。")

    btn_reset = tk.Button(btn_bar, text="恢复默认", command=reset_defaults, bg=CARD2, fg=TXT,
                          activebackground=BTN_H, activeforeground="white", relief="flat",
                          bd=0, padx=14, pady=4, font=F, cursor="hand2",
                          highlightthickness=1, highlightbackground=BORDER)
    btn_reset.pack(side="left", padx=(0, 10))
    btn_album = tk.Button(btn_bar, text="游戏相册", command=open_game_album, bg=CARD2, fg=TXT,
                          activebackground=BTN_H, activeforeground="white", relief="flat",
                          bd=0, padx=14, pady=4, font=F, cursor="hand2",
                          highlightthickness=1, highlightbackground=BORDER)
    btn_album.pack(side="left")

    main_frame.columnconfigure(0, weight=1)
    _load_settings()  # 恢复上次的勾选/账号ID/路径（放在所有函数定义之后，联动逻辑已可用）

    # 主窗口大小防抖记忆：resize 结束 300ms 后写入 settings
    _geo_after = [None]
    def _on_root_configure(_e):
        if _geo_after[0] is not None:
            root.after_cancel(_geo_after[0])
        _geo_after[0] = root.after(300, _save_settings)
    root.bind("<Configure>", _on_root_configure, add="+")

    say("就绪。密钥 57C43A21 循环 XOR；magic " + MAGIC.decode() + "。")
    say("来源目录建议：C:\\Users\\<用户>\\AppData\\Local\\NGR\\Saved\\GameAlbum")
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        sys.argv.pop(1)
        _cli()
    else:
        _run_gui()
