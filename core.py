# -*- coding: utf-8 -*-
"""
GameAlbum（王者荣耀世界）APD / APT 图片转换核心模块
纯 bytes 输入输出，适配 Pyodide 网页环境
"""
import struct
import json
import random
import string
import time
import io

KEY = b"\x57\xC4\x3A\x21"
MAGIC = b"GameAlbumHeader0.5"
ENC_JPEG_HEAD = b"\xA8\x1C\xC5\xC1"
TYPE_IMAGE = b"\x0F\x00\x00\x00"
TAIL_THUMB_SIZE = (400, 225)


def xor_xform(data, key=KEY):
    out = bytearray(len(data))
    for i, b in enumerate(data):
        out[i] = b ^ key[i & 3]
    return bytes(out)


def _u32(v):
    return struct.pack("<I", v)


def _field(text):
    if isinstance(text, str):
        text = text.encode("utf-8")
    return _u32(len(text) + 1) + text + b"\x00"


def _parse_fields(raw, start=4):
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


def _locate_jpeg_seg(raw):
    p = 4
    while p + 4 <= len(raw):
        jl = struct.unpack("<I", raw[p:p + 4])[0]
        if not (0 < jl <= len(raw) - p - 4):
            break
        dec = xor_xform(raw[p + 4:p + 4 + min(jl, 16)])
        if dec.startswith(b"\xFF\xD8") and jl > 1000:
            return p, raw[:p]
        p = p + 4 + jl
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
    raise ValueError("未找到 JPEG 数据段")


def _patch_header_field(raw, idx, new_val):
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


def _gen_share_code():
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=12))


def _build_jump_json(filename, share_code, jump_type=85):
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


def build_header(filename, role="", account="", icon="LandmarkIcon",
                 map_name="", share_code=None, jump_type=None):
    head = bytearray(TYPE_IMAGE)
    head += _u32(len(MAGIC) + 1) + MAGIC + b"\x00"
    head += _field(filename)
    head += _field("")
    head += _field(role if role else "未命名")
    head += _field(account if account else "0")
    if icon == "CreditGotoHomeIcon":
        code = share_code or _gen_share_code()
        head += _field("家园拍照")
        head += _field("CreditGotoHomeIcon")
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


def make_thumbnail(img, size=TAIL_THUMB_SIZE):
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


def _image_to_jpeg_bytes(image_bytes, quality=100):
    from PIL import Image
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode != "RGB":
        img = img.convert("RGB")
    # 检测输入是否本身是 JPEG
    try:
        if image_bytes[:3] == b"\xFF\xD8\xFF":
            return image_bytes, img
    except Exception:
        pass
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue(), img


def _png_bytes(thumb):
    buf = io.BytesIO()
    thumb.save(buf, "PNG")
    return buf.getvalue()


# ================================================================
# 解包方向
# ================================================================

def parse_apd_bytes(raw):
    """解析 APD bytes，返回 (字段列表, JPEG bytes, 尾部PNG bytes或None)"""
    fields, _ = _parse_fields(raw)
    jpos, _ = _locate_jpeg_seg(raw)
    jl = struct.unpack("<I", raw[jpos:jpos + 4])[0]
    if not (0 < jl <= len(raw) - jpos - 4):
        raise ValueError("JPEG 长度字段异常")
    jpeg = xor_xform(raw[jpos + 4:jpos + 4 + jl])
    after = jpos + 4 + jl
    tail_png = None
    rest = raw[after:]
    if len(rest) >= 4:
        tl = struct.unpack("<I", rest[:4])[0]
        if 0 < tl <= len(rest) - 4:
            cand = rest[4:4 + tl]
            if xor_xform(cand).startswith(b"\x89PNG"):
                tail_png = xor_xform(cand)
    return fields, jpeg, tail_png


def apd_to_jpg_bytes(raw, export_tail=True):
    """APD bytes -> (JPG bytes, 尾部PNG bytes或None, 字段列表)"""
    fields, jpeg, tail_png = parse_apd_bytes(raw)
    return jpeg, (tail_png if export_tail else None), fields


def apd_to_png_bytes(raw, export_tail=True):
    """APD bytes -> (PNG bytes, 尾部PNG bytes或None, 字段列表)"""
    from PIL import Image
    fields, jpeg, tail_png = parse_apd_bytes(raw)
    img = Image.open(io.BytesIO(jpeg))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue(), (tail_png if export_tail else None), fields


def apt_to_png_bytes(raw):
    """APT bytes -> PNG/JPG bytes"""
    dec = xor_xform(raw)
    if dec.startswith(b"\x89PNG"):
        return dec, ".png"
    s = dec.find(b"\xFF\xD8\xFF")
    e = dec.rfind(b"\xFF\xD9")
    if s >= 0 and e > s:
        return dec[s:e + 2], ".jpg"
    raise ValueError("APT 解密后不是有效图片")


# ================================================================
# 打包方向
# ================================================================

def image_to_apd_bytes(image_bytes, account="", role="", map_name="",
                       quality=100, make_apt=True, add_tail=True,
                       icon="LandmarkIcon", filename=None):
    """图片 bytes -> (APD bytes, APT bytes或None, 文件名)"""
    from PIL import Image
    jpeg, img = _image_to_jpeg_bytes(image_bytes, quality)
    acct = account if account else "0"
    if not filename:
        filename = "{}_{}".format(acct, int(time.time() * 1000))
    head = build_header(filename, role=role, account=account,
                        icon=icon, map_name=map_name)
    body = bytearray()
    if icon == "CreditGotoHomeIcon":
        js = _build_jump_json(filename, _gen_share_code(), 85)
        body += _u32(len(js)) + js
    body += _u32(len(jpeg)) + xor_xform(jpeg)
    png = None
    if add_tail:
        png = _png_bytes(make_thumbnail(img))
        body += _u32(len(png)) + xor_xform(png)
    apt_bytes = None
    if make_apt:
        if png is None:
            png = _png_bytes(make_thumbnail(img))
        apt_bytes = xor_xform(png)
    return bytes(head) + bytes(body), apt_bytes, filename


def png_to_apt_bytes(png_bytes, keep_size=False):
    """PNG bytes -> APT bytes"""
    from PIL import Image
    img = Image.open(io.BytesIO(png_bytes))
    if img.mode != "RGB":
        img = img.convert("RGB")
    if keep_size or img.size == TAIL_THUMB_SIZE:
        png = png_bytes
    else:
        img = make_thumbnail(img, TAIL_THUMB_SIZE)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        png = buf.getvalue()
    return xor_xform(png)


def replace_apd_bytes(apd_bytes, image_bytes, quality=100,
                      update_tail=False, update_apt=True):
    """替换 APD 图片，返回 (新APD bytes, 新APT bytes或None, 字段列表)"""
    from PIL import Image
    raw = apd_bytes
    jpos, head_bytes = _locate_jpeg_seg(raw)
    jl = struct.unpack("<I", raw[jpos:jpos + 4])[0]
    if not (0 < jl <= len(raw) - jpos - 4):
        raise ValueError("JPEG 长度字段异常")
    old_tail = raw[jpos + 4 + jl:]

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
                    tail_png_size = Image.open(io.BytesIO(dec_cand)).size
                except Exception:
                    pass

    jpeg, img = _image_to_jpeg_bytes(image_bytes, quality)
    new_tail = old_tail
    if update_tail and tail_is_png:
        png = _png_bytes(make_thumbnail(img, size=tail_png_size or TAIL_THUMB_SIZE))
        new_tail = _u32(len(png)) + xor_xform(png)

    out_bytes = head_bytes + _u32(len(jpeg)) + xor_xform(jpeg) + new_tail

    apt_bytes = None
    if update_apt:
        apt_size = TAIL_THUMB_SIZE
        png = _png_bytes(make_thumbnail(img, size=apt_size))
        apt_bytes = xor_xform(png)

    fields, _, _ = parse_apd_bytes(out_bytes)
    return out_bytes, apt_bytes, fields


def make_apd_from_template_bytes(template_bytes, image_bytes, quality=100,
                                  make_apt=True):
    """用模板 APD 生成新 APD，返回 (新APD bytes, APT bytes或None, 文件名)"""
    from PIL import Image
    raw = template_bytes
    jpos, head_bytes = _locate_jpeg_seg(raw)
    jl = struct.unpack("<I", raw[jpos:jpos + 4])[0]
    old_tail = raw[jpos + 4 + jl:]
    jpeg, img = _image_to_jpeg_bytes(image_bytes, quality)
    fields, _, _ = parse_apd_bytes(raw)
    acct = fields[4] if len(fields) > 4 and fields[4] else "0"
    filename = "{}_{}".format(acct, int(time.time() * 1000))
    head_bytes = _patch_header_field(head_bytes, 1, filename)

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
    new_tail = old_tail
    out_bytes = head_bytes + _u32(len(jpeg)) + xor_xform(jpeg) + new_tail
    apt_bytes = xor_xform(png) if make_apt else None
    return out_bytes, apt_bytes, filename
