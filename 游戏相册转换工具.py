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
    加密数据是随机字节，直接用特征头 find 可能伪匹配，所以先从头部字段结束处
    按「可选JSON段 + JPEG段」结构逐段核对（解密后须以 FF D8 开头）。
    JPEG 可能紧贴文件末尾（无尾部），长度字段允许等于剩余长度。"""
    _, hlen = _parse_fields(raw)
    cand = [hlen]
    if hlen + 4 <= len(raw):
        L = struct.unpack("<I", raw[hlen:hlen + 4])[0]
        if 0 < L < len(raw) - hlen - 4:
            cand.append(hlen + 4 + L)
    for p in cand:
        if p + 4 > len(raw):
            continue
        jl = struct.unpack("<I", raw[p:p + 4])[0]
        if 0 < jl <= len(raw) - p - 4:
            dec = xor_xform(raw[p + 4:p + 4 + jl])
            if dec.startswith(b"\xFF\xD8"):
                return p, raw[:p]
    # 兜底：按特征字节搜索，且必须满足「长度合理 + 解密后是 JPEG」双重校验
    pos = raw.find(ENC_JPEG_HEAD)
    while pos >= 0:
        if pos >= 4:
            jl = struct.unpack("<I", raw[pos - 4:pos])[0]
            if 0 < jl <= len(raw) - pos:
                dec = xor_xform(raw[pos:pos + jl])
                if dec.startswith(b"\xFF\xD8"):
                    return pos - 4, raw[:pos - 4]
        nxt = raw.find(ENC_JPEG_HEAD, pos + 1)
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
                           quality=96, make_apt=True, add_tail=True):
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
    pos = raw.find(ENC_JPEG_HEAD)
    if pos < 0:
        raise ValueError("未找到加密 JPEG 数据段（文件可能不是 GameAlbum 图片 APD）")
    # 加密流前的 uint32 记录了 JPEG 的精确长度（原始格式如此）
    jlen = 0
    if pos >= 4:
        jlen = struct.unpack("<I", raw[pos - 4:pos])[0]
    if jlen and 0 < jlen <= len(raw) - pos:
        jpeg = xor_xform(raw[pos:pos + jlen])
        after = pos + jlen
    else:
        # 兜底：按 FF D8 .. FF D9 搜索
        dec = xor_xform(raw[pos:])
        s = dec.find(b"\xFF\xD8\xFF")
        e = dec.rfind(b"\xFF\xD9")
        if s < 0 or e <= s:
            raise ValueError("解密后的 JPEG 数据不完整")
        jpeg = dec[s:e + 2]
        after = pos + e + 2
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

def _image_to_jpeg(image_path, quality=96):
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
                 quality=96, make_apt=True, add_tail=True):
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


def replace_apd_image(apd_path, image_path, out_dir=None, quality=96,
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
        src_name = "converted"
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
    e.add_argument("--update-tail", action="store_true", help="同步更新尾部缩略图（默认原样保留）")
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
                                                 update_tail=args.update_tail,
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
    root.title("GameAlbum 图片转换工具（王者荣耀世界 APD/APT）")
    # 程序图标（窗口标题栏 / 任务栏）：优先用打包进 EXE 的 icon.ico
    try:
        _icon_path = os.path.join(
            getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "icon.ico")
        if os.path.exists(_icon_path):
            root.iconbitmap(_icon_path)
    except Exception:
        pass

    # ---- 配色与字体（现代化深色主题） ----
    BG, CARD, CARD2, DEEP = "#0B1220", "#141F33", "#1B2A44", "#0A1422"
    TXT, SUB = "#E6EDF7", "#7C8DB5"
    BTN, BTN_H, ACC = "#2563EB", "#3B82F6", "#4C8DFF"
    OK, OK_H = "#16A34A", "#22C55E"
    F = ("Microsoft YaHei UI", 12)
    F_B = ("Microsoft YaHei UI", 12, "bold")
    root.configure(bg=BG)
    root.minsize(700, 520)

    # ---- 主内容滚动容器：窗口缩小到放不下时出现滚动条，滚轮查看全部内容 ----
    main_canvas = tk.Canvas(root, bg=BG, highlightthickness=0)

    class SlimScrollbar(tk.Canvas):
        """细条式现代滚动条：深色轨道 + 圆角滑块 + 悬停高亮，支持拖动与翻页"""

        def __init__(self, master, command, **kw):
            super().__init__(master, width=10, bg=BG, highlightthickness=0,
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

    def _on_wheel(e):
        """滚轮滚动主内容；鼠标在自带滚动的控件（日志/下拉框等）上时让它们自己滚"""
        try:
            w = root.winfo_containing(e.x_root, e.y_root)
        except Exception:
            w = None
        if w is not None:
            cls = w.winfo_class()
            if isinstance(w, (tk.Text, tk.Listbox)) or cls in ("TCombobox", "Listbox", "Treeview"):
                return
        main_canvas.yview_scroll(int(-e.delta / 120) * 2, "units")
    root.bind_all("<MouseWheel>", _on_wheel)

    # ---- 标题区 ----
    tk.Label(main_frame, text="GameAlbum 图片转换工具", bg=BG, fg="#F8FAFC",
             font=("Microsoft YaHei UI", 20, "bold")).grid(row=0, column=0, columnspan=5, pady=(16, 2))
    tk.Label(main_frame, text="王者荣耀世界 APD / APT 相册格式互转 · 支持图片替换与缩略图同步更新",
             bg=BG, fg=SUB, font=("Microsoft YaHei UI", 11)).grid(row=1, column=0, columnspan=5, pady=(0, 10))

    # ---- 左上角：打开游戏相册目录 ----
    def open_game_album():
        d = var_game_dir.get().strip()
        if not d:
            d = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                             "NGR", "Saved", "GameAlbum")
        if os.path.isdir(d):
            os.startfile(d)
            say("已打开游戏相册目录：" + d)
        else:
            messagebox.showwarning("目录不存在",
                                   "游戏相册目录不存在：\n{}\n\n请先进入游戏截图生成相册，或检查目录设置。".format(d))

    btn_album = tk.Button(main_frame, text="游戏相册", command=open_game_album, bg="#1E3A5F",
                          fg="#93C5FD", activebackground="#1D4ED8", activeforeground="white",
                          relief="flat", bd=0, padx=14, pady=4, font=F,
                          cursor="hand2", highlightthickness=0)
    btn_album.place(x=20, y=18, anchor="nw")

    # ---- 免责声明（独立窗口，右上角入口） ----
    DISCLAIMER_TEXT = (
        "本工具通过逆向游戏文件格式开发，仅供学习与个人娱乐使用，请勿用于商业用途或传播侵权内容。\n\n"
        "游戏版本更新后，文件格式或加密密钥可能发生变化，导致本工具失效或转换结果异常，\n"
        "不保证长期可用，也无法对后续游戏版本兼容性负责。\n\n"
        "操作前请自行备份游戏文件；因使用本工具造成的数据丢失、文件损坏、账号异常等问题，作者不承担任何责任。\n\n"
        "修改游戏本地文件可能违反游戏用户协议，请自行评估风险后再使用。"
    )
    disclaim_win = {"w": None}

    def toggle_disclaimer():
        if disclaim_win["w"] is not None and disclaim_win["w"].winfo_exists():
            disclaim_win["w"].lift()
            disclaim_win["w"].focus_force()
            return
        win = tk.Toplevel(root)
        win.title("免责声明")
        win.configure(bg=BG)
        win.geometry("720x380")
        win.minsize(560, 300)
        tk.Label(win, text="免责声明", bg=BG, fg="#EF4444",
                 font=("Microsoft YaHei UI", 17, "bold")).pack(anchor="w", padx=18, pady=(14, 6))
        body = tk.Frame(win, bg=BG)
        body.pack(fill="both", expand=True, padx=18, pady=(0, 8))
        txt = tk.Text(body, height=10, state="disabled", font=("Microsoft YaHei UI", 12),
                      bg=DEEP, fg="#EF4444", relief="flat", padx=14, pady=10, wrap="word")
        txt.pack(side="left", fill="both", expand=True)
        hsb = tk.Scrollbar(body, command=txt.yview, bg=CARD, troughcolor=DEEP,
                           activebackground=ACC, relief="flat", bd=0)
        hsb.pack(side="right", fill="y")
        txt.configure(yscrollcommand=hsb.set)
        txt.configure(state="normal")
        txt.insert("end", DISCLAIMER_TEXT)
        txt.configure(state="disabled")
        tk.Button(win, text="关闭", command=win.destroy, bg=BTN, fg="white",
                  activebackground=BTN_H, activeforeground="white", relief="flat",
                  bd=0, padx=26, pady=6, font=F_B, cursor="hand2", highlightthickness=0
                  ).pack(anchor="e", padx=18, pady=(0, 14))
        disclaim_win["w"] = win
        win.protocol("WM_DELETE_WINDOW",
                     lambda: (disclaim_win.__setitem__("w", None), win.destroy()))

    btn_disclaim = tk.Button(main_frame, text="免责声明", command=toggle_disclaimer, bg="#7F1D1D",
                             fg="#FECACA", activebackground="#B91C1C", activeforeground="white",
                             relief="flat", bd=0, padx=14, pady=4, font=F,
                             cursor="hand2", highlightthickness=0)
    btn_disclaim.place(relx=1.0, x=-20, y=18, anchor="ne")

    # ---- 参数变量（供各功能面板使用） ----
    var_account = tk.StringVar(value="")
    # 界面显示中文，写入文件时仍用英文原值（游戏端识别依赖英文标识符）
    icon_map = {"地标截图": "LandmarkIcon", "家园拍照": "CreditGotoHomeIcon"}
    var_icon = tk.StringVar(value="地标截图")
    # 图标类型输入框显示用（勾模板时显示「跟随模板」，不污染 var_icon 实际值）
    _icon_display = tk.StringVar(value="地标截图")
    _icon_eb_ref = [None]    # 图标类型输入框
    _icon_btn_ref = [None]   # 图标类型 ▼ 按钮
    # 家园拍照型必须配真实分享码（只能来自游戏原生模板），否则游戏点开会卡死，
    # 所以选「家园拍照」时强制启用模板开关并锁定。
    _tpl_cb_ref = [None]
    _auto_tpl = [False]  # 标记模板是否为「选家园拍照时自动勾选」的

    def _icon_changed(*_):
        cb = _tpl_cb_ref[0]
        if cb is None:
            return
        try:
            if not cb.winfo_exists():
                return
        except Exception:
            return
        if icon_map.get(var_icon.get(), "LandmarkIcon") == "CreditGotoHomeIcon":
            # 家园拍照：若模板原本没勾，则自动勾选并记录（切回地标时自动取消）
            if not var_template.get():
                _auto_tpl[0] = True
            var_template.set(True)
            cb.config(state="disabled")
        else:
            # 地标截图：自动勾选的模板撤销掉，手动勾选的保留
            if _auto_tpl[0]:
                _auto_tpl[0] = False
                var_template.set(False)
            cb.config(state="normal")
        _template_changed()  # 图标切换后重算尾部/图标锁定状态

    var_icon.trace_add("write", _icon_changed)
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

    # ---- 设置持久化：勾选状态 / 账号ID / 路径 重启后自动恢复 ----
    # 配置文件保存在程序（EXE）同目录，方便查看与备份
    _SETTINGS_FILE = os.path.join(
        os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)),
        "游戏相册转换工具_settings.json")
    _SETTINGS_VARS = [
        # 模板类先恢复：图标恢复触发联动时 var_template 已是保存值，_auto_tpl 逻辑才正确
        ("var_template", var_template), ("var_template_path", var_template_path),
        ("var_account", var_account), ("var_icon", var_icon), ("var_apt", var_apt),
        ("var_tail", var_tail), ("var_tail_upd", var_tail_upd), ("var_png", var_png),
        ("var_jpg", var_jpg),
        ("var_tail_out", var_tail_out), ("var_apt_upd", var_apt_upd),
        ("var_backup", var_backup),
        ("var_copy_game", var_copy_game), ("var_game_dir", var_game_dir),
        ("var_keep_name", var_keep_name),
    ]

    def _save_settings(*_):
        """任何选项/输入变化时写入配置文件（失败静默，不影响使用）"""
        try:
            data = {n: v.get() for n, v in _SETTINGS_VARS}
            data["_last_dirs"] = _last_dirs  # 各功能区独立记忆的上次目录
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

    for _, v in _SETTINGS_VARS:
        v.trace_add("write", _save_settings)
    # 未勾模板时「附加尾部缩略图」强制关闭且不可开启（普通打包带全新尾部会变糊）
    _tail_cb_ref = [None]

    def _template_changed(*_):
        cb = _tail_cb_ref[0]
        # 图标类型仅在「模板被勾选 且 当前图标不是家园拍照」时锁定为“跟随模板”：
        # 家园拍照会自动勾选模板，但不能因此锁死图标，否则选完家园就没法改回地标了。
        is_home = icon_map.get(var_icon.get(), "LandmarkIcon") == "CreditGotoHomeIcon"
        lock_icon = var_template.get() and not is_home

        def _icon_state(disabled):
            for w in (_icon_eb_ref[0], _icon_btn_ref[0]):
                if w is not None:
                    try:
                        w.config(state="disabled" if disabled else "normal")
                    except Exception:
                        pass
            try:
                _icon_display.set("跟随模板" if disabled else var_icon.get())
            except Exception:
                pass

        if var_template.get():
            if cb is not None:
                try:
                    cb.config(state="normal")
                except Exception:
                    pass
            _icon_state(lock_icon)
        else:
            var_tail.set(False)
            if cb is not None:
                try:
                    cb.config(state="disabled")
                except Exception:
                    pass
            _icon_state(False)

    var_template.trace_add("write", _template_changed)

    def _entry(parent, var, w=16):
        e = tk.Entry(parent, textvariable=var, width=w, bg=DEEP, fg="#F1F5F9",
                     insertbackground="#F1F5F9", relief="flat", font=F)
        e.configure(highlightthickness=1, highlightbackground="#334155", highlightcolor=ACC)
        return e

    # ---- 日志卡片（say 依赖 log，先建） ----
    lcard = tk.Frame(main_frame, bg=CARD, padx=16, pady=10)
    lcard.grid(row=4, column=0, columnspan=5, padx=18, pady=(12, 0), sticky="nsew")
    tk.Label(lcard, text="运行日志", bg=CARD, fg=TXT, font=F_B
             ).grid(row=0, column=0, sticky="w", pady=(0, 4))
    log = tk.Text(lcard, height=5, state="disabled", font=("Consolas", 9),
                  bg=DEEP, fg="#7DD3FC", insertbackground="#E2E8F0",
                  relief="flat", padx=8, pady=6, wrap="word")
    log.grid(row=1, column=0, sticky="nsew")
    sb = tk.Scrollbar(lcard, command=log.yview, bg=CARD, troughcolor=DEEP,
                      activebackground=ACC, relief="flat", bd=0)
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

    def _game_dir_init():
        """游戏相册目录：存在则用它，不存在则回退到用户主目录（避免文件对话框乱跳）"""
        d = var_game_dir.get().strip()
        if d and os.path.isdir(d):
            return d
        return os.path.expanduser("~")

    def outdir_for(paths):
        if not paths:
            return None
        d = os.path.join(os.path.dirname(paths[0]), "converted")
        os.makedirs(d, exist_ok=True)
        return d

    def do_apd2jpg():
        fs = pick_files("选择 APD 文件", [("GameAlbum 截图", "*.APD"), ("所有文件", "*.*")],
                        initialdir=_last_dirs.get("apd2jpg") or _game_dir_init())
        if not fs: return
        _last_dirs["apd2jpg"] = os.path.dirname(fs[0]); _save_settings()
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
                        initialdir=_last_dirs.get("apd2png") or _game_dir_init())
        if not fs: return
        _last_dirs["apd2png"] = os.path.dirname(fs[0]); _save_settings()
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
        tpl = var_template_path.get().strip() if var_template.get() else ""
        if var_template.get() and (not tpl or not os.path.isfile(tpl)):
            if not tpl:
                messagebox.showwarning("需要模板", "已勾选「使用游戏原生截图 APD 作模板」，请先点击「选择模板…」选一张游戏原生截图。")
            else:
                messagebox.showwarning("模板无效", "模板文件不存在或路径无效：\n{}\n请重新选择。".format(tpl))
            return
        od = outdir_for(fs)
        game_dir = var_game_dir.get().strip() if var_copy_game.get() else None
        n_ok = n_fail = n_copy = 0
        for f in fs:
            try:
                fname = None
                if var_keep_name.get():
                    fname = os.path.splitext(os.path.basename(f))[0]
                if tpl:
                    apd, apt = make_apd_from_template(tpl, f, od, filename=fname,
                                                       make_apt=var_apt.get(), add_tail=var_tail.get())
                    say("图片→APD(模板)  {}  →  {}{}".format(
                        os.path.basename(f), os.path.basename(apd),
                        "  + " + os.path.basename(apt) if apt else ""))
                else:
                    apd, apt = image_to_apd(f, od, filename=fname, account=var_account.get(), role="", map_name="",
                                            icon=icon_map.get(var_icon.get(), "LandmarkIcon"),
                                            make_apt=var_apt.get(), add_tail=var_tail.get())
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
        if n_fail:
            messagebox.showwarning("完成（有失败）", "成功 {} 个，失败 {} 个（详见下方日志）。\n输出目录：{}".format(n_ok, n_fail, od))
        else:
            msg = "转换完成，共 {} 个。输出目录：\n{}".format(n_ok, od)
            if n_copy:
                msg += "\n\n已复制 {} 个到游戏相册目录：\n{}".format(n_copy, game_dir)
                msg += "\n（游戏目录中的同名旧文件已自动备份到「复制备份_时间戳」文件夹）"
            messagebox.showinfo("完成", msg)

    def do_png2apt():
        fs = pick_files("选择 PNG 缩略图", [("PNG 图片", "*.png"), ("所有文件", "*.*")],
                        initialdir=_last_dirs.get("png2apt"))
        if not fs: return
        _last_dirs["png2apt"] = os.path.dirname(fs[0]); _save_settings()
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
                          initialdir=_game_dir_init())
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
                                                     update_tail=var_tail_upd.get(),
                                                     update_apt=var_apt_upd.get())
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
        ("图片 → APD", "把 JPG/PNG 打包成游戏格式", "img2apd"),
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
        def __init__(self, widget, text):
            self.widget = widget
            self.text = text
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
                           bg=CARD2, fg=TXT, font=("Microsoft YaHei UI", 12),
                           padx=12, pady=10, wraplength=400,
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
        ToolTip(cb, tip)
        h = tk.Label(fr, text="(?)", bg=CARD, fg=ACC, font=("Microsoft YaHei UI", 12, "bold"),
                     cursor="question_arrow")
        h.pack(side="left", padx=(4, 0))
        ToolTip(h, tip)
        return cb

    def _pick_template():
        fs = pick_files("选择游戏原生 APD 模板（游戏内截图的相册）",
                        [("GameAlbum 截图", "*.APD"), ("所有文件", "*.*")],
                        initialdir=_game_dir_init())
        if fs:
            var_template_path.set(fs[0])
            _last_dirs["img2apd"] = os.path.dirname(fs[0]); _save_settings()
            say("已选模板：" + os.path.basename(fs[0]))

    def build_opts(kid):
        for w in opt_frame.winfo_children():
            w.destroy()
        tk.Label(opt_frame, text="参数（仅当前功能使用）", bg=CARD, fg=TXT, font=F_B
                 ).grid(row=0, column=0, columnspan=8, sticky="w", pady=(0, 8))
        if kid == "apd2jpg":
            _opt(1, "同时输出 PNG 主图（默认只出 JPG）", var_png,
                 "解包 APD 时除 JPG 主图外，再额外输出一份 PNG 格式的主图。\n不勾则只输出 JPG。")
            _opt(2, "导出尾部缩略图（_thumb.png）", var_tail_out,
                 "额外解出 APD 内部尾部嵌着的小缩略图，保存为 _thumb.png。\n不勾则只解主图。")
            _w_hint = tk.Label(opt_frame, text="提示：转换后的文件输出在所选 APD 所在目录的 converted 子文件夹中。",
                               bg=CARD, fg=SUB, font=("Microsoft YaHei UI", 11))
            _w_hint.grid(row=3, column=0, columnspan=8, sticky="w", padx=2, pady=(8, 0))
        elif kid == "apd2png":
            _opt(1, "同时输出 JPG 主图（默认只出 PNG）", var_jpg,
                 "解包 APD 时除 PNG 主图外，再额外输出一份 JPG 格式的主图。\n不勾则只输出 PNG。")
            _opt(2, "导出尾部缩略图（_thumb.png）", var_tail_out,
                 "额外解出 APD 内部尾部嵌着的小缩略图，保存为 _thumb.png。\n不勾则只解主图。")
            _w_hint = tk.Label(opt_frame, text="提示：转换后的文件输出在所选 APD 所在目录的 converted 子文件夹中。",
                               bg=CARD, fg=SUB, font=("Microsoft YaHei UI", 11))
            _w_hint.grid(row=3, column=0, columnspan=8, sticky="w", padx=2, pady=(8, 0))
        elif kid == "img2apd":
            row1f = tk.Frame(opt_frame, bg=CARD)
            row1f.grid(row=1, column=0, columnspan=8, sticky="w", pady=3)
            tk.Label(row1f, text="账号ID（留空=0）", bg=CARD, fg=SUB, font=F
                     ).pack(side="left", padx=(2, 6))
            _entry(row1f, var_account, 9).pack(side="left")
            tk.Label(row1f, text="图标类型", bg=CARD, fg=SUB, font=F
                     ).pack(side="left", padx=(8, 6))
            # 输入框 + ▼ 箭头放进同一个容器，保证绝对紧贴
            icon_row = tk.Frame(row1f, bg=CARD)
            icon_row.pack(side="left")
            e_icon = tk.Entry(icon_row, textvariable=_icon_display, state="readonly", width=9,
                              bg=DEEP, fg=TXT, readonlybackground=DEEP, relief="flat",
                              font=F, justify="center")
            e_icon.configure(highlightthickness=1, highlightbackground="#334155", highlightcolor=ACC)
            e_icon.pack(side="left")
            _icon_eb_ref[0] = e_icon

            def _icon_menu_open(e=e_icon):
                m = tk.Menu(opt_frame, tearoff=0, bg=DEEP, fg=TXT,
                            activebackground=BTN, activeforeground="white",
                            bd=0, relief="flat", font=F)
                for k in icon_map:
                    def _sel(kk=k):
                        var_icon.set(kk)
                        _icon_display.set(kk)
                    m.add_command(label=k, command=_sel)
                try:
                    # 菜单直接从输入框正下方展开，左对齐输入框
                    m.tk_popup(e.winfo_rootx(), e.winfo_rooty() + e.winfo_height())
                finally:
                    m.grab_release()

            b_icon = tk.Button(icon_row, text="▼", command=_icon_menu_open,
                               bg=CARD2, fg=SUB, activebackground=BTN, activeforeground="white",
                               relief="flat", bd=0, padx=6, font=F, cursor="hand2", highlightthickness=0)
            b_icon.pack(side="left", padx=(2, 0))
            _icon_btn_ref[0] = b_icon
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
            ToolTip(cb_apt, _apt_tip)
            h_apt = tk.Label(fr_apt, text="(?)", bg=CARD, fg=ACC, font=("Microsoft YaHei UI", 12, "bold"),
                             cursor="question_arrow")
            h_apt.pack(side="left", padx=(4, 0))
            ToolTip(h_apt, _apt_tip)
            fr_tail = tk.Frame(row2f, bg=CARD)
            fr_tail.pack(side="left", padx=(16, 0))
            cb_tail = tk.Checkbutton(fr_tail, text="附加尾部缩略图", variable=var_tail,
                                     bg=CARD, fg=TXT, selectcolor=DEEP, activebackground=CARD,
                                     activeforeground=TXT, font=F, relief="flat", bd=0, highlightthickness=0)
            cb_tail.pack(side="left")
            _tail_tip = ("在 APD 内部尾部嵌入一张小缩略图。\n"
                         "仅勾选「使用游戏原生截图 APD 作模板」时可开启；\n"
                         "不勾模板时强制关闭（普通打包的全新尾部会导致游戏点开图片变糊）。")
            ToolTip(cb_tail, _tail_tip)
            h_tail = tk.Label(fr_tail, text="(?)", bg=CARD, fg=ACC, font=("Microsoft YaHei UI", 12, "bold"),
                              cursor="question_arrow")
            h_tail.pack(side="left", padx=(4, 0))
            ToolTip(h_tail, _tail_tip)
            _tail_cb_ref[0] = cb_tail
            fr_name = tk.Frame(row2f, bg=CARD)
            fr_name.pack(side="left", padx=(16, 0))
            cb_name = tk.Checkbutton(fr_name, text="使用原文件名", variable=var_keep_name,
                                     bg=CARD, fg=TXT, selectcolor=DEEP, activebackground=CARD,
                                     activeforeground=TXT, font=F, relief="flat", bd=0, highlightthickness=0)
            cb_name.pack(side="left")
            _name_tip = ("生成的 APD/APT 使用原图片的文件名（不含扩展名）命名，方便辨认。\n"
                         "默认关闭（默认用 账号ID_时间戳 命名）。\n"
                         "注意：游戏相册对文件名有识别规则，使用原文件名可能影响游戏内识别，请自行实测。")
            ToolTip(cb_name, _name_tip)
            h_name = tk.Label(fr_name, text="(?)", bg=CARD, fg=ACC, font=("Microsoft YaHei UI", 12, "bold"),
                              cursor="question_arrow")
            h_name.pack(side="left", padx=(4, 0))
            ToolTip(h_name, _name_tip)
            _tpl_cb_ref[0] = _opt(3, "使用游戏原生截图 APD 作模板", var_template,
                 "勾选后，以一张游戏原生截图作模板：完整复用其头部（真实分享码/昵称/账号/地图/JSON），"
                 "只替换图片。这样打包出的 APD 游戏相册里能正常识别、点开清晰不卡死。\n"
                 "注意：图标类型选「家园拍照」时此开关自动勾选且不可取消（家园型必须带真实分享码，"
                 "否则游戏点开会卡死）。",
                 col=0, colspan=4, padx=2, pady=3)
            _icon_changed()
            _template_changed()
            row4f = tk.Frame(opt_frame, bg=CARD)
            row4f.grid(row=4, column=0, columnspan=8, sticky="we", pady=3)
            tk.Label(row4f, text="模板 APD（游戏原生截图）", bg=CARD, fg=SUB, font=F).pack(side="left", padx=(2, 6))
            _entry(row4f, var_template_path, 30).pack(side="left", fill="x", expand=True)
            tk.Button(row4f, text="选择模板…", width=10,
                      command=lambda: _pick_template(),
                      bg=CARD2, fg=TXT, activebackground=BTN, activeforeground="white",
                      relief="flat", bd=0, padx=8, font=F, cursor="hand2", highlightthickness=0
                      ).pack(side="left", padx=(4, 0))
            _opt(5, "同时复制到游戏相册（需重启游戏生效）", var_copy_game,
                 "打包完成后把生成的 APD/APT 自动复制到游戏相册目录（路径在下方输入框，可修改）。\n"
                 "同名旧文件自动备份到「复制备份_时间戳」文件夹。\n"
                 "注意：使用模板打包时无需填写账号ID（自动取模板的账号）。",
                 col=0, colspan=4, padx=2, pady=(4, 0))
            row6f = tk.Frame(opt_frame, bg=CARD)
            row6f.grid(row=6, column=0, columnspan=8, sticky="we", pady=3)
            tk.Label(row6f, text="游戏相册目录", bg=CARD, fg=SUB, font=F).pack(side="left", padx=(2, 6))
            _entry(row6f, var_game_dir, 30).pack(side="left", fill="x", expand=True)
            tk.Button(row6f, text="浏览目录…", width=10,
                      command=lambda: var_game_dir.set(
                          filedialog.askdirectory(initialdir=_game_dir_init()) or var_game_dir.get()),
                      bg=CARD2, fg=TXT, activebackground=BTN, activeforeground="white",
                      relief="flat", bd=0, padx=8, font=F, cursor="hand2", highlightthickness=0
                      ).pack(side="left", padx=(4, 0))
            _w_copy = tk.Label(opt_frame, text="勾选后，生成的 APD/APT 会同时复制到该目录（同名旧文件自动备份到「复制备份_时间戳」）。",
                               bg=CARD, fg=SUB, font=("Microsoft YaHei UI", 9))
            _w_copy.grid(row=7, column=0, columnspan=4, sticky="w", padx=2, pady=(0, 2))
        elif kid == "replace":
            _opt(1, "同步更新尾部缩略图（默认保留原尾部）", var_tail_upd,
                 "替换 APD 时，把内部尾部的小缩略图也换成新图。\n不勾则保留原 APD 的尾部小图。")
            _opt(2, "同时替换同名 APT 缩略图文件", var_apt_upd,
                 "替换 APD 时，同名 .APT 文件也换成新缩略图（推荐勾选）。\n不勾则 APT 保持原样。")
            _opt(3, "保留备份（替换前备份原文件）", var_backup,
                 "开启（默认）：替换前把原 APD 及同名 APT 备份到「替换备份」文件夹，可随时复制回原位恢复。\n"
                 "关闭：直接覆盖，不留任何备份，原文件无法找回。")
            _w_note = tk.Label(opt_frame, text="说明：先选要替换的 APD（不选 APT），再选新图片；替换在原位置直接覆盖，原文件自动备份到「替换备份」文件夹。",
                               bg=CARD, fg=SUB, font=("Microsoft YaHei UI", 11))
            _w_note.grid(row=4, column=0, columnspan=8, sticky="w", padx=2, pady=(2, 0))
        else:
            tk.Label(opt_frame, text="此功能无需参数，点击下方按钮直接开始。",
                     bg=CARD, fg=SUB, font=F).grid(row=1, column=0, columnspan=8, sticky="w", padx=2)
        tk.Button(opt_frame, text="开始转换", command=lambda: _start(kid), bg=OK, fg="white",
                  activebackground=OK_H, activeforeground="white", relief="flat", bd=0,
                  padx=22, pady=6, font=F_B, cursor="hand2", highlightthickness=0
                  ).grid(row=9, column=0, columnspan=8, pady=(12, 4), sticky="w")
        opt_frame.grid()

    def select_feat(kid):
        if sel["kid"] == kid:
            # 再点同一张卡片 → 收起参数面板（再点才展开）
            sel["kid"] = None
            for k in cards:
                f, t, d = cards[k]
                f.configure(highlightbackground="#263450")
                t.configure(fg=TXT)
                d.configure(fg=SUB)
            opt_frame.grid_remove()
            # 参数面板收起后立即刷新滚动范围（顶部从 0 起，无空白富余）
            root.after(30, lambda: main_canvas.configure(
                scrollregion=(0, 0, main_frame.winfo_width(), main_frame.winfo_reqheight())))
            return
        sel["kid"] = kid
        for k in cards:
            f, t, d = cards[k]
            on = (k == kid)
            f.configure(highlightbackground=ACC if on else "#263450")
            t.configure(fg=ACC if on else TXT)
            d.configure(fg=ACC if on else SUB)
        build_opts(kid)

    # 6 张卡片：3 行 × 2 列，grid 均分（uniform 保证两列等宽、sticky 拉伸占满）
    for i, (t, d, kid) in enumerate(feats):
        r, c = divmod(i, 2)
        f = tk.Frame(bcard, bg=CARD2, highlightthickness=1, highlightbackground="#263450")
        f.grid(row=1 + r, column=c, padx=6, pady=5, sticky="ew")
        tl = tk.Label(f, text=t, bg=CARD2, fg=TXT, font=("Microsoft YaHei UI", 14, "bold"),
                      cursor="hand2")
        tl.pack(anchor="w", padx=12, pady=(10, 0))
        dl = tk.Label(f, text=d, bg=CARD2, fg=SUB, font=("Microsoft YaHei UI", 11), cursor="hand2")
        dl.pack(anchor="w", padx=12, pady=(0, 10))
        for w in (f, tl, dl):
            w.bind("<Button-1>", lambda _e, k=kid: select_feat(k))
        cards[kid] = (f, tl, dl)
    bcard.columnconfigure(0, weight=1, uniform="fc")
    bcard.columnconfigure(1, weight=1, uniform="fc")

    # ---- 使用说明（独立窗口） ----
    HELP_TEXT = (
        "【游戏相册位置】\n"
        "游戏相册目录默认在：C:\\Users\\用户名\\AppData\\Local\\NGR\\Saved\\GameAlbum（用户名 = 你电脑的登录用户名）。\n"
        "游戏在本地自动生成，你游戏里拍的截图 / 录制的相册都存放在这里。\n"
        "如需改路径，可在「图片 → APD」参数里用「浏览目录…」按钮修改。\n\n"
        "【文件格式说明】\n"
        "· APD：游戏相册主文件，里面记录截图信息（账号 / 昵称 / 地图 / 分享码）+ 加密的全尺寸大图（JPEG 格式）+ 尾部缩略图。"
        "它用了 57 C4 3A 21 循环异或加密，双击打不开、显示乱码是正常的，用本工具解包即可。\n"
        "· APT：相册缩略图文件，是相册列表里显示的小图，与 APD 同名成对出现，可单独解包或打包。\n"
        "· JPG：有损压缩图片，文件小、加载快，适合查看与分享。\n"
        "· PNG：无损压缩图片，画质完全保留、文件较大，适合留档与二次编辑。\n\n"
        "【功能与选项说明】\n"
        "① APD → JPG：提取主图，输出 JPG 原图\n"
        "  勾选「同时输出 PNG 主图」：解出的图额外存一份 PNG 无损版（画质全保留，文件更大）。\n"
        "  勾选「导出尾部缩略图（_thumb.png）」：把 APD 尾部内嵌的小图也导出成一个独立 PNG。\n\n"
        "② APD → PNG：提取主图，输出 PNG 无损格式\n"
        "  勾选「同时输出 JPG 主图」：额外存一份 JPG（文件更小，适合分享）。\n"
        "  勾选「导出尾部缩略图（_thumb.png）」：同①，把尾部内嵌小图也导出。\n\n"
        "③ PNG → APT：把 PNG 打包成缩略图文件\n"
        "  自动缩放到 400×225（游戏列表小图的标准尺寸），适合自制缩略图。\n\n"
        "④ APT → PNG：把缩略图文件解成 PNG\n"
        "  原样输出，不缩放。\n\n"
        "⑤ 图片 → APD：把 JPG/PNG 打包成游戏相册文件\n"
        "  账号ID：相册归属的账号标识（留空自动写 0；填游戏账号 ID 可让相册归到该账号）。\n"
        "  图标类型：相册在游戏里显示的图标（地标截图 / 家园拍照）。\n"
        "  勾选「使用游戏原生截图 APD 作模板」（推荐）：先点「选择模板…」选一张游戏原生截图，"
        "工具会完整复用它的头部（真实分享码/昵称/账号/地图/JSON），只替换图片，"
        "这样打包出的 APD 游戏相册里能正常识别、点开清晰不卡死；"
        "不勾则用普通方式打包（家园型带随机分享码，游戏可能校验失败卡死；地标型可能模糊）。\n"
        "  勾选「同时生成 APT 缩略图」：打包时顺带生成同名 .APT 缩略图文件（供列表显示）。\n"
        "  勾选「附加尾部缩略图」：在 APD 内部尾部嵌入一张小缩略图。仅勾选「使用游戏原生截图 APD 作模板」时可开启；"
        "不勾模板时强制关闭（普通打包生成的全新尾部，游戏可能校验失败，点开图片变糊）。\n"
        "  勾选「使用原文件名」：生成的 APD/APT 用原图片的文件名（不含扩展名）命名，方便辨认；"
        "默认关闭（默认用 账号ID_时间戳 命名）。注意游戏相册对文件名有识别规则，使用原文件名可能影响游戏内识别，请自行实测。\n"
        "  勾选「同时复制到游戏相册」：打包完成后把生成的 APD/APT 自动复制到游戏相册目录（路径可在输入框修改），"
        "游戏目录里的同名旧文件会自动备份到「复制备份_时间戳」文件夹。使用模板打包时无需填账号ID（自动取模板的账号）。\n\n"
        "⑥ 替换 APD 图片：只换图，保留全部原有信息（账号 / 昵称 / 地图 / 分享码 / 文件名）\n"
        "  流程：先选要替换的 APD（注意不要选 .APT）→ 再选新图片 → 确认 → 在原位置直接覆盖；"
        "原 APD 与同名 APT 自动备份到「替换备份」文件夹（多次操作的备份合并存放，同名自动加时间戳）。\n"
        "  勾选「同步更新尾部缩略图」：APD 内部尾部的小图也换成新图（不勾则保留原尾部）。\n"
        "  勾选「同时替换同名 APT 缩略图文件」：同名 .APT 文件也换成新缩略图（默认勾选，"
        "不勾则只替换 APD、APT 保持原样）。\n"
        "  勾选「保留备份」（默认开启）：替换前把原文件备份到「替换备份」文件夹，可随时恢复；"
        "关闭则直接覆盖、不留任何备份。\n\n"
        "【通用说明】\n"
        "· 输出默认在源文件所在目录的 converted 子文件夹（替换功能例外：原位覆盖 + 自动备份）。\n"
        "· 所有勾选、账号ID、路径等设置会自动保存（保存在程序同目录的「游戏相册转换工具_settings.json」），重启后自动恢复。\n"
        "· 各功能区的「开始转换」文件选择框独立记忆各自的目录（重启后保留）；「APD→JPG」「APD→PNG」和「替换第一步」默认定位到游戏相册目录，"
        "路径不存在时自动回退到用户主目录；发给别人使用时，会自动改用对方电脑上的游戏相册路径。\n"
        "· 「替换 APD 图片」第二步（选新图片）也独立记忆上次目录。\n"
        "· 主界面右下角「恢复默认」按钮：一键清空所有设置与目录记忆，恢复为默认状态。\n"
        "· 打包 / 替换后的文件结构与原版一致，但游戏能否识别需进游戏实测。\n"
        "· 「替换备份」文件夹里的文件是操作前的原始文件，可随时复制回原位恢复。\n\n"
    )
    help_win = {"w": None}

    def toggle_help():
        if help_win["w"] is not None and help_win["w"].winfo_exists():
            help_win["w"].lift()
            help_win["w"].focus_force()
            return
        win = tk.Toplevel(root)
        win.title("使用说明")
        win.configure(bg=BG)
        win.geometry("800x860")
        win.minsize(600, 520)
        tk.Label(win, text="使用说明", bg=BG, fg="#F8FAFC",
                 font=("Microsoft YaHei UI", 17, "bold")).pack(anchor="w", padx=18, pady=(14, 6))
        body = tk.Frame(win, bg=BG)
        body.pack(fill="both", expand=True, padx=18, pady=(0, 8))
        txt = tk.Text(body, height=30, state="disabled", font=("Microsoft YaHei UI", 12),
                      bg=DEEP, fg="#CBD5E1", relief="flat", padx=14, pady=10, wrap="word")
        txt.pack(side="left", fill="both", expand=True)
        hsb = tk.Scrollbar(body, command=txt.yview, bg=CARD, troughcolor=DEEP,
                           activebackground=ACC, relief="flat", bd=0)
        hsb.pack(side="right", fill="y")
        txt.configure(yscrollcommand=hsb.set)
        txt.configure(state="normal")
        txt.insert("end", HELP_TEXT)
        txt.configure(state="disabled")
        tk.Button(win, text="关闭", command=win.destroy, bg=BTN, fg="white",
                  activebackground=BTN_H, activeforeground="white", relief="flat",
                  bd=0, padx=26, pady=6, font=F_B, cursor="hand2", highlightthickness=0
                  ).pack(anchor="e", padx=18, pady=(0, 14))
        help_win["w"] = win
        win.protocol("WM_DELETE_WINDOW",
                     lambda: (help_win.__setitem__("w", None), win.destroy()))

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
                                           "能否被游戏识别需进游戏实测。\n输出默认在源文件目录的 converted 子文件夹；"
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
            "var_account": "", "var_icon": "地标截图", "var_apt": True, "var_tail": False,
            "var_tail_upd": False, "var_png": False, "var_jpg": False, "var_tail_out": False,
            "var_apt_upd": True, "var_backup": True, "var_copy_game": True, "var_game_dir": os.path.join(
                os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "NGR", "Saved", "GameAlbum"),
            "var_template": False, "var_template_path": "", "var_keep_name": False,
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
                          bd=0, padx=14, pady=4, font=F, cursor="hand2", highlightthickness=0)
    btn_reset.pack(side="left", padx=(0, 10))
    btn_help = tk.Button(btn_bar, text="使用说明", command=lambda: toggle_help(), bg=CARD2, fg=TXT,
                         activebackground=BTN_H, activeforeground="white", relief="flat",
                         bd=0, padx=14, pady=4, font=F, cursor="hand2", highlightthickness=0)
    btn_help.pack(side="left")

    main_frame.columnconfigure(0, weight=1)
    main_frame.rowconfigure(4, weight=1)
    _load_settings()  # 恢复上次的勾选/账号ID/路径（放在所有函数定义之后，联动逻辑已可用）
    say("就绪。密钥 57C43A21 循环 XOR；magic " + MAGIC.decode() + "。")
    say("来源目录建议：C:\\Users\\<用户>\\AppData\\Local\\NGR\\Saved\\GameAlbum")
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        sys.argv.pop(1)
        _cli()
    else:
        _run_gui()
