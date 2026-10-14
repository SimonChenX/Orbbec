import cv2
from PIL import Image, ImageDraw, ImageFont

f = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 30)
a = cv2.imread('/tmp/obshot/tune_before.jpg')
b = cv2.imread('/tmp/obshot/tune_after.jpg')
H = 560


def fit(im):
    s = H / float(im.shape[0])
    return cv2.resize(im, (int(im.shape[1] * s), H), interpolation=cv2.INTER_AREA)


a, b = fit(a), fit(b)
W = a.shape[1] + b.shape[1] + 30
canvas = Image.new("RGB", (W, H + 46), (16, 18, 22))
d = ImageDraw.Draw(canvas)
canvas.paste(Image.fromarray(cv2.cvtColor(a, cv2.COLOR_BGR2RGB)), (10, 40))
canvas.paste(Image.fromarray(cv2.cvtColor(b, cv2.COLOR_BGR2RGB)), (a.shape[1] + 20, 40))
d.text((10, 8), "BEFORE (device default)", fill=(255, 120, 120), font=f)
d.text((a.shape[1] + 20, 8), "AFTER (tuned)", fill=(120, 255, 140), font=f)
canvas.save('/tmp/obshot/tune_compare.jpg', quality=90)
print("saved", canvas.size)