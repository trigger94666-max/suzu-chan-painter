"""画一个跑图台图标（蓝鲸+女仆头饰）→ suzune.ico"""
import os

from PIL import Image, ImageDraw

S = 512
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 圆角底板
d.rounded_rectangle([8, 8, S - 8, S - 8], radius=110, fill=(23, 32, 44, 255))
d.rounded_rectangle([8, 8, S - 8, S - 8], radius=110, outline=(74, 159, 224, 255), width=10)

# 鲸鱼身体（椭圆）
body = [90, 190, 400, 380]
d.ellipse(body, fill=(74, 159, 224, 255))
# 肚皮
d.ellipse([130, 300, 380, 392], fill=(190, 225, 248, 255))

# 尾巴（两片）
d.polygon([(392, 258), (472, 196), (452, 286)], fill=(74, 159, 224, 255))
d.polygon([(392, 300), (478, 344), (440, 366)], fill=(74, 159, 224, 255))

# 眼睛
d.ellipse([158, 236, 204, 282], fill=(255, 255, 255, 255))
d.ellipse([172, 250, 198, 276], fill=(20, 26, 34, 255))
d.ellipse([181, 256, 190, 265], fill=(255, 255, 255, 255))

# 腮红
d.ellipse([120, 292, 162, 320], fill=(240, 150, 170, 150))

# 女仆头饰（白色荷叶边）
d.arc([150, 130, 360, 250], start=200, end=340, fill=(255, 255, 255, 255), width=26)
for i in range(5):
    cx = 190 + i * 36
    d.ellipse([cx - 20, 138 + abs(2 - i) * 6, cx + 20, 178 + abs(2 - i) * 6],
              fill=(255, 255, 255, 255))

# 小水花
d.ellipse([86, 150, 118, 182], fill=(120, 190, 235, 160))
d.ellipse([372, 130, 396, 154], fill=(120, 190, 235, 120))

_HERE = os.path.dirname(os.path.abspath(__file__))
img.save(os.path.join(_HERE, "suzune.ico"),
         sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.resize((256, 256), Image.LANCZOS).save(os.path.join(_HERE, "suzune_preview.png"))
print("saved suzune.ico + suzune_preview.png")
