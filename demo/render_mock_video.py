#!/usr/bin/env python3
"""Render a customer-facing mock video for Villa Agency OS.

No production data. No external dependency beyond Pillow + ffmpeg.
"""
from __future__ import annotations

import math
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "demo" / "villa-agency-os-mock-video.mp4"
THUMB = ROOT / "demo" / "villa-agency-os-mock-video-cover.png"
FRAMES = Path("/tmp/villa_agency_mock_video_frames")
W, H = 1280, 720
FPS = 24
SCENE_SECONDS = 4
BG = "#eef3f5"
NAVY = "#123146"
BLUE = "#1f648f"
GOLD = "#9a6515"
GREEN = "#23694a"
RED = "#a13e3a"
INK = "#17232b"
MUTED = "#667580"
LINE = "#d9e1e5"
PAPER = "#ffffff"


def font(size: int, bold: bool = False):
    names = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for name in names:
        if Path(name).exists():
            return ImageFont.truetype(name, size)
    return ImageFont.load_default()

F12, F16, F20, F24, F32, F48 = font(12), font(16), font(20), font(24), font(32, True), font(48, True)
F64 = font(64, True)


def rr(d: ImageDraw.ImageDraw, xy, radius, fill, outline=None, width=1):
    d.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


def text(d, xy, s, fill=INK, f=F20, anchor=None):
    d.text(xy, s, font=f, fill=fill, anchor=anchor)


def ease(t: float) -> float:
    return 1 - (1 - t) * (1 - t)


def wrap_lines(draw, s, fnt, max_w):
    words = s.split()
    lines, cur = [], ""
    for w in words:
        nxt = (cur + " " + w).strip()
        if draw.textlength(nxt, font=fnt) <= max_w:
            cur = nxt
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def sidebar(d):
    rr(d, (0, 0, 244, H), 0, NAVY)
    rr(d, (34, 38, 88, 92), 16, "#e7bd62")
    text(d, (61, 55), "VA", NAVY, F24, "ma")
    text(d, (104, 42), "Villa Agency", "#ffffff", F24)
    text(d, (104, 72), "OS Demo", "#bad2de", F16)
    items = ["Command", "Pipeline", "Inventory", "Finance", "Messages", "Owner"]
    for i, item in enumerate(items):
        y = 138 + i * 54
        fill = "#23495f" if i == min(max(current_scene-1, 0), 5) else NAVY
        rr(d, (22, y, 222, y + 40), 12, fill)
        rr(d, (38, y + 15, 48, y + 25), 5, "#f0c76c" if fill != NAVY else "#6fa8c7")
        text(d, (60, y + 10), item, "#e9f5fa", F16)
    text(d, (28, 642), "Mock demo — data contoh", "#bad2de", F12)
    text(d, (28, 662), "Bali + Yogyakarta", "#ffffff", F16)


def card(d, xy, title, value, note, color=GREEN):
    x1, y1, x2, y2 = xy
    rr(d, xy, 18, PAPER, LINE)
    text(d, (x1 + 22, y1 + 18), title, MUTED, F16)
    text(d, (x1 + 22, y1 + 48), value, INK, F48)
    rr(d, (x1 + 22, y2 - 38, x1 + 180, y2 - 14), 12, "#e9f6ee" if color == GREEN else "#fff4dc")
    text(d, (x1 + 34, y2 - 33), note, color, F12)


def draw_header(d, title, subtitle):
    text(d, (284, 36), title, INK, F48)
    text(d, (286, 94), subtitle, MUTED, F20)


def draw_progress(d, pct):
    rr(d, (284, 666, 1194, 678), 8, "#dfe7eb")
    rr(d, (284, 666, 284 + int(910 * pct), 678), 8, GREEN)


def scene_intro(d, t):
    rr(d, (0,0,W,H), 0, NAVY)
    text(d, (82, 74), "VILLA MANAGEMENT & AGENCY", "#cde3ed", F20)
    text(d, (82, 143), "Dari inquiry ke booking", "#ffffff", F64)
    text(d, (82, 220), "tanpa double booking, tanpa uang tercecer, tanpa klaim AI yang tidak punya bukti.", "#d8ebf2", F24)
    rr(d, (82, 318, 404, 374), 28, "#e7bd62")
    text(d, (104, 334), "Mock video untuk sales demo", NAVY, F20)
    for i, label in enumerate(["Reservation", "Finance", "Operations", "Owner Audit"]):
        x = 82 + i*286
        rr(d, (x, 468, x+238, 566), 22, "#173a50", "#2f6078")
        text(d, (x+22, 492), label, "#ffffff", F24)
        text(d, (x+22, 530), "satu sistem", "#bcd3df", F16)


def scene_command(d, t):
    sidebar(d); draw_header(d, "Command Center", "Antrean reservation, finance, operations, approval, dan audit dalam satu layar.")
    card(d, (284, 142, 494, 270), "Double booking", "0", "blocked")
    card(d, (514, 142, 724, 270), "Open inquiries", "3", "next action")
    card(d, (744, 142, 954, 270), "Pending approvals", "1", "human review", GOLD)
    card(d, (974, 142, 1184, 270), "Card data stored", "0", "hard gate")
    rr(d, (284, 306, 1184, 622), 20, PAPER, LINE)
    text(d, (314, 332), "Hari ini: antrean paling mahal kalau telat", INK, F24)
    rows = [("Reservation", "Rahma minta Ubud 3 malam", "Quote grounded", GREEN), ("Finance", "Webhook pi_demo_01 cocok invoice", "Match", GREEN), ("Owner", "Approval kirim offer Villa Pantai", "Waiting", GOLD), ("Ops", "Siapkan check-in Canggu", "Assigned", BLUE)]
    for i, (a,b,c,col) in enumerate(rows):
        y=382+i*52
        d.line((314,y+38,1154,y+38), fill=LINE, width=1)
        text(d, (318,y), a, MUTED, F16); text(d, (500,y), b, INK, F16)
        rr(d, (950,y-3,1115,y+25), 14, "#e9f6ee" if col==GREEN else "#fff4dc" if col==GOLD else "#e8f3f9")
        text(d, (965,y+2), c, col, F12)
    draw_progress(d, .28)


def scene_pipeline(d, t):
    sidebar(d); draw_header(d, "Inquiry → Booking", "Chat tamu berubah menjadi case, matching, hold, invoice, dan tugas operasi.")
    labels = ["Pesan masuk", "Matching villa", "Hold slot", "Owner approve", "Booking + invoice"]
    for i, lab in enumerate(labels):
        x = 294 + i * 174
        active = t > i / 5
        rr(d, (x, 168, x+140, 250), 18, "#e9f6ee" if active else PAPER, GREEN if active else LINE)
        text(d, (x+70, 188), str(i+1), GREEN if active else MUTED, F32, "ma")
        for j,line in enumerate(wrap_lines(d, lab, F16, 112)):
            text(d, (x+70, 220+j*18), line, INK, F16, "ma")
        if i < 4:
            d.line((x+142, 209, x+172, 209), fill=GREEN if active else LINE, width=4)
    rr(d, (314, 330, 1152, 560), 22, PAPER, LINE)
    text(d, (346, 364), "Contoh deal", INK, F24)
    text(d, (346, 416), "Villa Pantai · Canggu · 3 malam", INK, F32)
    text(d, (346, 464), "Quote Rp 8.970.000 → Invoice issued → Check-in task dibuat", MUTED, F20)
    rr(d, (900, 394, 1100, 466), 36, "#e8f3f9")
    text(d, (1000, 431), "No spreadsheet", BLUE, F20, "ma")
    draw_progress(d, .44)


def scene_inventory(d, t):
    sidebar(d); draw_header(d, "Anti Double Booking", "Slot aktif tidak bisa dijual dua kali — state machine + constraint database.")
    rr(d, (300, 160, 1166, 560), 22, PAPER, LINE)
    headers = ["Unit", "Tanggal", "State", "Proteksi"]
    xs = [330, 570, 770, 950]
    for x,h in zip(xs,headers): text(d,(x,190),h,MUTED,F16)
    data = [("Villa Pantai", "12–15 Oct", "CONFIRMED", GREEN, "unique index"), ("Villa Sawah", "14–17 Oct", "HOLD_ACTIVE", GOLD, "blocks competing quote"), ("Kamar Prawiro", "20–22 Oct", "SELLABLE", BLUE, "available evidence")]
    for i,row in enumerate(data):
        y=242+i*86; d.line((326,y+54,1134,y+54), fill=LINE, width=1)
        text(d,(330,y),row[0],INK,F24); text(d,(570,y),row[1],INK,F20)
        rr(d,(770,y-3,920,y+31),17,"#e9f6ee" if row[3]==GREEN else "#fff4dc" if row[3]==GOLD else "#e8f3f9")
        text(d,(786,y+4),row[2],row[3],F12); text(d,(950,y),row[4],MUTED,F16)
    rr(d,(396,590,1066,640),25,"#fff4dc")
    text(d,(731,615),"Jika konflik muncul, sistem eskalasi. Bukan menjual diam-diam.",GOLD,F20,"ma")
    draw_progress(d, .60)


def scene_finance(d, t):
    sidebar(d); draw_header(d, "Finance & Reconciliation", "Uang dicocokkan dari bukti provider/bank — bukan screenshot WhatsApp.")
    card(d,(306,150,560,300),"Invoice issued","2","demo dataset")
    card(d,(590,150,844,300),"Unmatched payment","1","finance review",GOLD)
    card(d,(874,150,1128,300),"PAN / CVV stored","0","blocked")
    rr(d,(306,338,1128,570),22,PAPER,LINE)
    text(d,(338,370),"Payment event pi_demo_01",INK,F24)
    text(d,(338,418),"Signature verified · amount Rp 8.970.000 · invoice matched",MUTED,F20)
    rr(d,(338,474,600,526),26,"#e9f6ee"); text(d,(469,501),"MATCHED",GREEN,F24,"ma")
    rr(d,(634,474,1060,526),26,"#fff0ee"); text(d,(846,501),"Card data never enters database",RED,F20,"ma")
    draw_progress(d, .74)


def scene_messages(d, t):
    sidebar(d); draw_header(d, "Safe Message Approval", "Draft keluar hanya kalau setiap angka/tanggal punya sumber dan manusia menyetujui.")
    rr(d,(300,156,760,560),22,PAPER,LINE)
    text(d,(330,190),"Draft ke tamu",INK,F24)
    para="Halo Rahma, Villa Sawah tersedia 2026-10-14 sampai 2026-10-17. Tarif Rp 1.500.000 per malam, total Rp 4.950.000."
    y=244
    for line in wrap_lines(d,para,F20,370):
        text(d,(330,y),line,INK,F20); y+=34
    rr(d,(330,470,548,518),24,"#e9f6ee"); text(d,(439,494),"GROUNDED",GREEN,F20,"ma")
    rr(d,(800,156,1160,560),22,PAPER,LINE)
    steps=[("1", "Agent creates draft", GREEN), ("2", "Owner approves", GOLD), ("3", "Agent records send proof", BLUE)]
    for i,(n,lab,col) in enumerate(steps):
        yy=220+i*92
        rr(d,(838,yy,884,yy+46),23,"#e9f6ee" if col==GREEN else "#fff4dc" if col==GOLD else "#e8f3f9")
        text(d,(861,yy+23),n,col,F20,"ma"); text(d,(908,yy+10),lab,INK,F20)
    draw_progress(d, .88)


def scene_close(d, t):
    rr(d,(0,0,W,H),0,"#f7f9fa")
    text(d,(80,64),"Demo siap ditunjukkan",NAVY,F64)
    text(d,(82,148),"Mock aplikasi + video ini memakai data contoh. Untuk live production tetap perlu keputusan bisnis.",MUTED,F24)
    items=[("Software gate", "PASS", GREEN), ("Demo dataset", "RUNNABLE", GREEN), ("Business go-live", "NEEDS DECISION", GOLD)]
    for i,(a,b,col) in enumerate(items):
        x=82+i*376
        rr(d,(x,264,x+320,450),24,PAPER,LINE)
        text(d,(x+28,304),a,MUTED,F20); text(d,(x+28,356),b,col,F32)
    rr(d,(82,542,1198,620),36,NAVY)
    text(d,(640,581),"Next: isi harga paket + pilih provider + pilot 10–20 properti", "#ffffff", F24, "ma")

SCENES=[scene_intro,scene_command,scene_pipeline,scene_inventory,scene_finance,scene_messages,scene_close]
current_scene=0


def render():
    if FRAMES.exists(): shutil.rmtree(FRAMES)
    FRAMES.mkdir(parents=True)
    total=0
    global current_scene
    for si, func in enumerate(SCENES):
        current_scene=si
        for f in range(SCENE_SECONDS*FPS):
            t=f/(SCENE_SECONDS*FPS-1)
            img=Image.new("RGB",(W,H),BG)
            d=ImageDraw.Draw(img)
            func(d,ease(t))
            # scene fade-in/out
            fade=1.0
            if f<8: fade=f/8
            if f>SCENE_SECONDS*FPS-9: fade=(SCENE_SECONDS*FPS-1-f)/8
            if fade<1:
                overlay=Image.new("RGB",(W,H),BG)
                img=Image.blend(overlay,img,max(0,min(1,fade)))
            img.save(FRAMES/f"frame_{total:05d}.png")
            if si==0 and f==18:
                img.save(THUMB)
            total+=1
    cmd=["ffmpeg","-y","-framerate",str(FPS),"-i",str(FRAMES/"frame_%05d.png"),"-c:v","libx264","-pix_fmt","yuv420p","-movflags","+faststart",str(OUT)]
    subprocess.run(cmd,check=True)
    print(OUT)

if __name__=="__main__":
    render()
