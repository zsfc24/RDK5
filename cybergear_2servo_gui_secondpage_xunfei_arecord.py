import serial
import serial.tools.list_ports
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading
import time
import json
import subprocess
import shutil

import math
import re
import base64
import hashlib
import hmac
import ssl
from email.utils import formatdate
from urllib.parse import urlencode

try:
    import pyttsx3
except Exception:
    pyttsx3 = None

try:
    import websocket
except Exception:
    websocket = None



# 讯飞“中英识别大模型”WebAPI：控制台对应页面里的 APPID / APIKey / APISecret
XUNFEI_IAT_HOST = "iat.xf-yun.com"
XUNFEI_IAT_PATH = "/v1"



class CyberGear2ServoGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("CyberGear + 双 PUL/DIR 伺服控制工具")
        self.root.geometry("1500x900")
        self.root.minsize(1200, 720)

        self.ser = serial.Serial()
        self.is_open = False
        self.rx_buffer = ""
        self.tx_lock = threading.Lock()

        self.action_thread = None
        self.action_stop_event = threading.Event()
        self.action_running = False

        self.voice_thread = None
        self.voice_stop_event = threading.Event()
        self.voice_ws = None
        self.voice_running = False
        self.voice_text_parts = []
        self.speaker_lock = threading.Lock()
        self.speaker_engine = None
        if pyttsx3 is not None:
            try:
                self.speaker_engine = pyttsx3.init()
                self.speaker_engine.setProperty("rate", 180)
                self.speaker_engine.setProperty("volume", 1.0)
            except Exception:
                self.speaker_engine = None

        self.setup_ui()

        try:
            self.root.state("zoomed")
        except Exception:
            pass

    def setup_ui(self):
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=4, pady=4)

        self.page_original = ttk.Frame(self.notebook)
        self.page_pingpong = ttk.Frame(self.notebook)

        self.notebook.add(self.page_original, text="第一页：原控制页")
        self.notebook.add(self.page_pingpong, text="第二页：乒乓球发球机")

        main = ttk.PanedWindow(self.page_original, orient=tk.HORIZONTAL)
        main.pack(fill="both", expand=True, padx=8, pady=8)

        left_outer = ttk.Frame(main)
        right_outer = ttk.LabelFrame(main, text="通信消息 / 接收发送日志", padding=8)

        main.add(left_outer, weight=3)
        main.add(right_outer, weight=2)

        canvas = tk.Canvas(left_outer, highlightthickness=0)
        left_scroll = ttk.Scrollbar(left_outer, orient="vertical", command=canvas.yview)
        self.controls = ttk.Frame(canvas)

        self.controls.bind(
            "<Configure>",
            lambda event: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        canvas_window = canvas.create_window((0, 0), window=self.controls, anchor="nw")

        def resize_controls(event):
            canvas.itemconfigure(canvas_window, width=event.width)

        canvas.bind("<Configure>", resize_controls)
        canvas.configure(yscrollcommand=left_scroll.set)
        canvas.pack(side=tk.LEFT, fill="both", expand=True)
        left_scroll.pack(side=tk.RIGHT, fill="y")

        def on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind_all("<MouseWheel>", on_mousewheel)

        self.log_text = tk.Text(
            right_outer,
            bg="#1e1e1e",
            fg="#d4d4d4",
            insertbackground="#ffffff",
            font=("Consolas", 10),
            wrap=tk.NONE
        )
        self.log_text.pack(side=tk.LEFT, fill="both", expand=True)

        scroll = ttk.Scrollbar(right_outer, orient="vertical", command=self.log_text.yview)
        scroll.pack(side=tk.RIGHT, fill="y")
        self.log_text.configure(yscrollcommand=scroll.set)

        parent = self.controls

        top = ttk.LabelFrame(parent, text="主串口", padding=8)
        top.pack(fill="x", padx=4, pady=6)

        line1 = ttk.Frame(top)
        line1.pack(fill="x", pady=3)

        ttk.Label(line1, text="串口:").pack(side=tk.LEFT, padx=3)
        self.port_cb = ttk.Combobox(line1, width=14, state="readonly")
        self.port_cb.pack(side=tk.LEFT, padx=3)

        ttk.Button(line1, text="刷新", command=self.refresh_port).pack(side=tk.LEFT, padx=3)

        self.open_btn = ttk.Button(line1, text="打开串口", command=self.open_close_port)
        self.open_btn.pack(side=tk.LEFT, padx=3)

        ttk.Button(line1, text="STOP总停", command=lambda: self.send_main("STOP")).pack(side=tk.LEFT, padx=3)
        ttk.Button(line1, text="CLEAR", command=lambda: self.send_main("CLEAR")).pack(side=tk.LEFT, padx=3)

        line2 = ttk.Frame(top)
        line2.pack(fill="x", pady=3)

        ttk.Label(line2, text="自定义指令:").pack(side=tk.LEFT, padx=3)
        self.cmd_var = tk.StringVar()
        cmd_entry = ttk.Entry(line2, textvariable=self.cmd_var, width=38)
        cmd_entry.pack(side=tk.LEFT, padx=3, fill="x", expand=True)
        cmd_entry.bind("<Return>", lambda event: self.send_from_entry())

        ttk.Button(line2, text="发送", command=self.send_from_entry).pack(side=tk.LEFT, padx=3)

        comm = ttk.LabelFrame(parent, text="通信选择 / 总线工具", padding=8)
        comm.pack(fill="x", padx=4, pady=6)

        self.comm_var = tk.StringVar(value="MAIN")

        ttk.Radiobutton(comm, text="MAIN", variable=self.comm_var, value="MAIN",
                        command=self.send_comm_select).pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(comm, text="M1 / CAN1", variable=self.comm_var, value="CAN1",
                        command=self.send_comm_select).pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(comm, text="M2 / CAN2", variable=self.comm_var, value="CAN2",
                        command=self.send_comm_select).pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(comm, text="ALL", variable=self.comm_var, value="ALL",
                        command=self.send_comm_select).pack(side=tk.LEFT, padx=5)

        ttk.Button(comm, text="查询选择", command=lambda: self.send_main("COMM?")).pack(side=tk.LEFT, padx=5)
        ttk.Button(comm, text="CAN状态", command=lambda: self.send_main("CAN?")).pack(side=tk.LEFT, padx=5)
        ttk.Button(comm, text="扫描电机", command=lambda: self.send_main("SCAN")).pack(side=tk.LEFT, padx=5)

        setid = ttk.LabelFrame(parent, text="电机 ID 设置：建议一次只接一个默认 ID=127 的电机", padding=8)
        setid.pack(fill="x", padx=4, pady=6)

        ttk.Label(setid, text="旧ID:").pack(side=tk.LEFT, padx=3)
        self.old_id_var = tk.IntVar(value=127)
        ttk.Entry(setid, textvariable=self.old_id_var, width=8).pack(side=tk.LEFT, padx=3)

        ttk.Label(setid, text="新ID:").pack(side=tk.LEFT, padx=3)
        self.new_id_var = tk.IntVar(value=1)
        ttk.Entry(setid, textvariable=self.new_id_var, width=8).pack(side=tk.LEFT, padx=3)

        ttk.Button(setid, text="设置ID", command=self.send_set_id).pack(side=tk.LEFT, padx=5)
        ttk.Button(setid, text="设为M1: 127->1", command=lambda: self.quick_set_id(127, 1)).pack(side=tk.LEFT, padx=5)
        ttk.Button(setid, text="设为M2: 127->2", command=lambda: self.quick_set_id(127, 2)).pack(side=tk.LEFT, padx=5)

        motor = ttk.LabelFrame(parent, text="CyberGear 基础控制", padding=8)
        motor.pack(fill="x", padx=4, pady=6)

        row_motor = ttk.Frame(motor)
        row_motor.pack(fill="x", pady=4)

        ttk.Label(row_motor, text="电机:").pack(side=tk.LEFT, padx=3)
        self.motor_id = tk.IntVar(value=1)
        ttk.Combobox(row_motor, textvariable=self.motor_id, values=[1, 2],
                     width=6, state="readonly").pack(side=tk.LEFT, padx=3)

        ttk.Button(row_motor, text="选择通信+使能", command=self.quick_select_enable).pack(side=tk.LEFT, padx=3)
        ttk.Button(row_motor, text="使能", command=self.send_enable).pack(side=tk.LEFT, padx=3)
        ttk.Button(row_motor, text="停止", command=self.send_stop_motor).pack(side=tk.LEFT, padx=3)
        ttk.Button(row_motor, text="设机械零点", command=self.send_zero).pack(side=tk.LEFT, padx=3)
        ttk.Button(row_motor, text="查询状态", command=self.send_status).pack(side=tk.LEFT, padx=3)

        mode_frame = ttk.LabelFrame(parent, text="原生模式切换 run_mode：0 运控 / 1 位置 / 2 速度 / 3 电流", padding=8)
        mode_frame.pack(fill="x", padx=4, pady=6)

        ttk.Button(mode_frame, text="运控模式 0", command=lambda: self.send_mode(0)).pack(side=tk.LEFT, padx=4)
        ttk.Button(mode_frame, text="位置模式 1", command=lambda: self.send_mode(1)).pack(side=tk.LEFT, padx=4)
        ttk.Button(mode_frame, text="速度模式 2", command=lambda: self.send_mode(2)).pack(side=tk.LEFT, padx=4)
        ttk.Button(mode_frame, text="电流模式 3", command=lambda: self.send_mode(3)).pack(side=tk.LEFT, padx=4)

        pos_frame = ttk.LabelFrame(parent, text="原生位置模式：MxPOS目标位置mrad,最大速度mrad/s", padding=8)
        pos_frame.pack(fill="x", padx=4, pady=6)

        row_pos = ttk.Frame(pos_frame)
        row_pos.pack(fill="x", pady=4)

        ttk.Label(row_pos, text="目标位置 mrad:").pack(side=tk.LEFT, padx=3)
        self.pos_mrad = tk.IntVar(value=1000)
        ttk.Entry(row_pos, textvariable=self.pos_mrad, width=12).pack(side=tk.LEFT, padx=3)

        ttk.Label(row_pos, text="最大速度 mrad/s:").pack(side=tk.LEFT, padx=3)
        self.pos_speed_mrad_s = tk.IntVar(value=444)
        ttk.Entry(row_pos, textvariable=self.pos_speed_mrad_s, width=12).pack(side=tk.LEFT, padx=3)

        ttk.Button(row_pos, text="原生位置运行 MxPOS", command=self.send_native_position).pack(side=tk.LEFT, padx=6)
        ttk.Button(row_pos, text="M1 到 1rad@0.444rad/s", command=lambda: self.quick_pos(1, 1000, 444)).pack(side=tk.LEFT, padx=4)
        ttk.Button(row_pos, text="M2 到 1rad@0.444rad/s", command=lambda: self.quick_pos(2, 1000, 444)).pack(side=tk.LEFT, padx=4)
        ttk.Button(row_pos, text="当前电机回零", command=lambda: self.quick_pos(self.motor_id.get(), 0, 444)).pack(side=tk.LEFT, padx=4)

        spd_frame = ttk.LabelFrame(parent, text="原生速度模式：MxSPD速度mrad/s", padding=8)
        spd_frame.pack(fill="x", padx=4, pady=6)

        row_spd = ttk.Frame(spd_frame)
        row_spd.pack(fill="x", pady=4)

        ttk.Label(row_spd, text="速度 mrad/s:").pack(side=tk.LEFT, padx=3)
        self.speed_mrad_s = tk.IntVar(value=1000)
        ttk.Entry(row_spd, textvariable=self.speed_mrad_s, width=12).pack(side=tk.LEFT, padx=3)

        ttk.Button(row_spd, text="原生速度运行 MxSPD", command=self.send_native_speed).pack(side=tk.LEFT, padx=6)
        ttk.Button(row_spd, text="速度设 0", command=lambda: self.send_main(f"{self.current_motor_prefix()}SPD0")).pack(side=tk.LEFT, padx=4)
        ttk.Button(row_spd, text="M1 1rad/s", command=lambda: self.quick_speed(1, 1000)).pack(side=tk.LEFT, padx=4)
        ttk.Button(row_spd, text="M2 1rad/s", command=lambda: self.quick_speed(2, 1000)).pack(side=tk.LEFT, padx=4)
        ttk.Button(row_spd, text="M2 -1rad/s", command=lambda: self.quick_speed(2, -1000)).pack(side=tk.LEFT, padx=4)

        current_frame = ttk.LabelFrame(parent, text="原生电流模式：MxIQ 电流mA，谨慎使用", padding=8)
        current_frame.pack(fill="x", padx=4, pady=6)

        ttk.Label(current_frame, text="Iq mA:").pack(side=tk.LEFT, padx=3)
        self.iq_ma = tk.IntVar(value=0)
        ttk.Entry(current_frame, textvariable=self.iq_ma, width=12).pack(side=tk.LEFT, padx=3)
        ttk.Button(current_frame, text="发送电流 MxIQ", command=self.send_current).pack(side=tk.LEFT, padx=6)

        limit_frame = ttk.LabelFrame(parent, text="目标角度软件限位：MxL最小mrad,最大mrad", padding=8)
        limit_frame.pack(fill="x", padx=4, pady=6)

        ttk.Label(limit_frame, text="最小:").pack(side=tk.LEFT, padx=3)
        self.limit_min = tk.IntVar(value=-3000)
        ttk.Entry(limit_frame, textvariable=self.limit_min, width=10).pack(side=tk.LEFT, padx=3)

        ttk.Label(limit_frame, text="最大:").pack(side=tk.LEFT, padx=3)
        self.limit_max = tk.IntVar(value=3000)
        ttk.Entry(limit_frame, textvariable=self.limit_max, width=10).pack(side=tk.LEFT, padx=3)

        ttk.Button(limit_frame, text="设置当前电机限位", command=self.send_limit).pack(side=tk.LEFT, padx=5)
        ttk.Button(limit_frame, text="查询限位", command=lambda: self.send_main("LIMIT?")).pack(side=tk.LEFT, padx=5)
        ttk.Button(limit_frame, text="M1 ±3rad", command=lambda: self.quick_limit(1, -3000, 3000)).pack(side=tk.LEFT, padx=4)
        ttk.Button(limit_frame, text="M2 ±3rad", command=lambda: self.quick_limit(2, -3000, 3000)).pack(side=tk.LEFT, padx=4)

        servo_frame = ttk.LabelFrame(parent, text="两路 PUL/DIR 伺服电机：S1=PA8/PB12，S2=PB6/PB13", padding=8)
        servo_frame.pack(fill="x", padx=4, pady=6)

        row_servo1 = ttk.Frame(servo_frame)
        row_servo1.pack(fill="x", pady=4)

        ttk.Label(row_servo1, text="伺服:").pack(side=tk.LEFT, padx=3)
        self.servo_id = tk.IntVar(value=1)
        ttk.Combobox(row_servo1, textvariable=self.servo_id, values=[1, 2],
                     width=6, state="readonly").pack(side=tk.LEFT, padx=3)

        ttk.Label(row_servo1, text="目标位置/指令单位:").pack(side=tk.LEFT, padx=3)
        self.servo_pos_cmd = tk.IntVar(value=100)
        ttk.Entry(row_servo1, textvariable=self.servo_pos_cmd, width=12).pack(side=tk.LEFT, padx=3)

        ttk.Label(row_servo1, text="速度周期us/越小越快:").pack(side=tk.LEFT, padx=3)
        self.servo_speed_us = tk.IntVar(value=500)
        ttk.Entry(row_servo1, textvariable=self.servo_speed_us, width=12).pack(side=tk.LEFT, padx=3)

        ttk.Button(row_servo1, text="设置速度 SxS", command=self.send_servo_speed).pack(side=tk.LEFT, padx=5)
        ttk.Button(row_servo1, text="绝对位置 SxP", command=self.send_servo_position).pack(side=tk.LEFT, padx=5)
        ttk.Button(row_servo1, text="当前位置设零 SxZ", command=self.send_servo_zero).pack(side=tk.LEFT, padx=5)
        ttk.Button(row_servo1, text="停止 SxSTOP", command=self.send_servo_stop).pack(side=tk.LEFT, padx=5)

        row_servo2 = ttk.Frame(servo_frame)
        row_servo2.pack(fill="x", pady=4)

        ttk.Button(row_servo2, text="S1 到 100", command=lambda: self.quick_servo_pos(1, 100)).pack(side=tk.LEFT, padx=4)
        ttk.Button(row_servo2, text="S1 回零", command=lambda: self.quick_servo_pos(1, 0)).pack(side=tk.LEFT, padx=4)
        ttk.Button(row_servo2, text="S2 到 100", command=lambda: self.quick_servo_pos(2, 100)).pack(side=tk.LEFT, padx=4)
        ttk.Button(row_servo2, text="S2 回零", command=lambda: self.quick_servo_pos(2, 0)).pack(side=tk.LEFT, padx=4)
        ttk.Button(row_servo2, text="S1/S2 全停", command=lambda: self.send_main("SSTOP")).pack(side=tk.LEFT, padx=8)

        ttk.Label(
            servo_frame,
            text="STM32 指令：S1P100 / S2P100 走绝对位置；S1S500 / S2S500 设置脉冲周期；S1Z / S2Z 当前位置设零。位置比例：10个指令单位=1mm。"
        ).pack(anchor="w", pady=2)

        self.build_action_loop_ui(parent)

        self.refresh_port()
        self.log("启动成功：右侧为黑色通信窗口，左侧为可滚动控制区。\n")
        self.build_pingpong_page(self.page_pingpong)


    def build_action_loop_ui(self, parent):
        action_frame = ttk.LabelFrame(parent, text="动作循环编辑器：按顺序发送指令，可编辑/保存/循环执行", padding=8)
        action_frame.pack(fill="both", padx=4, pady=6)

        edit_row = ttk.Frame(action_frame)
        edit_row.pack(fill="x", pady=3)

        ttk.Label(edit_row, text="指令:").pack(side=tk.LEFT, padx=3)
        self.action_cmd_var = tk.StringVar(value="S1P100")
        ttk.Entry(edit_row, textvariable=self.action_cmd_var, width=22).pack(side=tk.LEFT, padx=3)

        ttk.Label(edit_row, text="发送后等待ms:").pack(side=tk.LEFT, padx=3)
        self.action_delay_var = tk.StringVar(value="1000")
        ttk.Entry(edit_row, textvariable=self.action_delay_var, width=10).pack(side=tk.LEFT, padx=3)

        ttk.Label(edit_row, text="备注:").pack(side=tk.LEFT, padx=3)
        self.action_note_var = tk.StringVar()
        ttk.Entry(edit_row, textvariable=self.action_note_var, width=22).pack(side=tk.LEFT, padx=3, fill="x", expand=True)

        btn_row = ttk.Frame(action_frame)
        btn_row.pack(fill="x", pady=3)
        ttk.Button(btn_row, text="添加步骤", command=self.add_action_row).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_row, text="更新选中", command=self.update_action_row).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_row, text="删除选中", command=self.delete_action_row).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_row, text="上移", command=lambda: self.move_action_row(-1)).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_row, text="下移", command=lambda: self.move_action_row(1)).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_row, text="清空", command=self.clear_actions).pack(side=tk.LEFT, padx=3)

        quick_row = ttk.Frame(action_frame)
        quick_row.pack(fill="x", pady=3)
        ttk.Button(quick_row, text="添加当前Cyber位置", command=self.add_current_cyber_position_action).pack(side=tk.LEFT, padx=3)
        ttk.Button(quick_row, text="添加当前Cyber速度", command=self.add_current_cyber_speed_action).pack(side=tk.LEFT, padx=3)
        ttk.Button(quick_row, text="添加当前伺服位置", command=self.add_current_servo_position_action).pack(side=tk.LEFT, padx=3)
        ttk.Button(quick_row, text="添加STOP总停", command=lambda: self.add_action_row("STOP", 1000, "总停")).pack(side=tk.LEFT, padx=3)
        ttk.Button(quick_row, text="导入示例", command=self.load_demo_actions).pack(side=tk.LEFT, padx=3)

        table_frame = ttk.Frame(action_frame)
        table_frame.pack(fill="both", expand=True, pady=4)

        columns = ("cmd", "delay", "note")
        self.action_tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=8)
        self.action_tree.heading("cmd", text="指令")
        self.action_tree.heading("delay", text="等待ms")
        self.action_tree.heading("note", text="备注")
        self.action_tree.column("cmd", width=180, anchor="w")
        self.action_tree.column("delay", width=80, anchor="center")
        self.action_tree.column("note", width=260, anchor="w")
        self.action_tree.pack(side=tk.LEFT, fill="both", expand=True)
        action_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.action_tree.yview)
        action_scroll.pack(side=tk.RIGHT, fill="y")
        self.action_tree.configure(yscrollcommand=action_scroll.set)
        self.action_tree.bind("<<TreeviewSelect>>", self.on_action_selected)

        run_row = ttk.Frame(action_frame)
        run_row.pack(fill="x", pady=4)
        ttk.Label(run_row, text="循环次数:").pack(side=tk.LEFT, padx=3)
        self.action_repeat_var = tk.StringVar(value="1")
        ttk.Entry(run_row, textvariable=self.action_repeat_var, width=8).pack(side=tk.LEFT, padx=3)
        ttk.Label(run_row, text="0=无限循环").pack(side=tk.LEFT, padx=3)

        self.action_start_btn = ttk.Button(run_row, text="开始循环", command=self.start_action_loop)
        self.action_start_btn.pack(side=tk.LEFT, padx=8)
        self.action_stop_btn = ttk.Button(run_row, text="停止循环", command=lambda: self.stop_action_loop(False), state=tk.DISABLED)
        self.action_stop_btn.pack(side=tk.LEFT, padx=3)
        ttk.Button(run_row, text="停止循环+STOP", command=lambda: self.stop_action_loop(True)).pack(side=tk.LEFT, padx=3)
        ttk.Button(run_row, text="保存动作", command=self.save_actions_to_file).pack(side=tk.LEFT, padx=8)
        ttk.Button(run_row, text="载入动作", command=self.load_actions_from_file).pack(side=tk.LEFT, padx=3)

        self.action_status_var = tk.StringVar(value="空闲")
        ttk.Label(run_row, textvariable=self.action_status_var).pack(side=tk.LEFT, padx=8)

        ttk.Label(
            action_frame,
            text="说明：每行是一条要发给 STM32 的串口指令，例如 COMM:ALL、M1POS1000,444、S1P100、STOP；等待时间表示发送该指令后暂停多久再发下一条。"
        ).pack(anchor="w", pady=2)

    def get_action_delay_ms(self):
        try:
            delay_ms = int(str(self.action_delay_var.get()).strip())
        except Exception:
            messagebox.showwarning("提示", "等待时间必须是整数，单位 ms")
            return None
        if delay_ms < 0:
            messagebox.showwarning("提示", "等待时间不能小于 0")
            return None
        return delay_ms

    def add_action_row(self, cmd=None, delay_ms=None, note=None):
        cmd = str(cmd if cmd is not None else self.action_cmd_var.get()).strip()
        if "\n" in cmd or "\r" in cmd:
            messagebox.showwarning("提示", "单条动作指令不能包含换行")
            return
        if not cmd:
            messagebox.showwarning("提示", "动作指令不能为空")
            return
        if delay_ms is None:
            delay_ms = self.get_action_delay_ms()
            if delay_ms is None:
                return
        else:
            try:
                delay_ms = int(delay_ms)
            except Exception:
                delay_ms = 0
        if delay_ms < 0:
            delay_ms = 0
        note = str(note if note is not None else self.action_note_var.get()).strip()
        self.action_tree.insert("", tk.END, values=(cmd, delay_ms, note))

    def on_action_selected(self, event=None):
        selected = self.action_tree.selection()
        if not selected:
            return
        cmd, delay_ms, note = self.action_tree.item(selected[0], "values")
        self.action_cmd_var.set(cmd)
        self.action_delay_var.set(str(delay_ms))
        self.action_note_var.set(note)

    def update_action_row(self):
        selected = self.action_tree.selection()
        if not selected:
            messagebox.showwarning("提示", "请先选中一个动作步骤")
            return
        cmd = self.action_cmd_var.get().strip()
        if "\n" in cmd or "\r" in cmd or not cmd:
            messagebox.showwarning("提示", "动作指令不能为空，也不能包含换行")
            return
        delay_ms = self.get_action_delay_ms()
        if delay_ms is None:
            return
        note = self.action_note_var.get().strip()
        self.action_tree.item(selected[0], values=(cmd, delay_ms, note))

    def delete_action_row(self):
        for item in self.action_tree.selection():
            self.action_tree.delete(item)

    def move_action_row(self, direction):
        selected = self.action_tree.selection()
        if not selected:
            return
        item = selected[0]
        parent = self.action_tree.parent(item)
        index = self.action_tree.index(item)
        new_index = index + direction
        children = self.action_tree.get_children(parent)
        if new_index < 0 or new_index >= len(children):
            return
        self.action_tree.move(item, parent, new_index)
        self.action_tree.selection_set(item)

    def clear_actions(self):
        if self.action_running:
            messagebox.showwarning("提示", "动作循环运行中，先停止再清空")
            return
        for item in self.action_tree.get_children():
            self.action_tree.delete(item)

    def add_current_cyber_position_action(self):
        self.add_action_row(f"{self.current_motor_prefix()}POS{self.pos_mrad.get()},{self.pos_speed_mrad_s.get()}", 1000, "CyberGear 位置")

    def add_current_cyber_speed_action(self):
        self.add_action_row(f"{self.current_motor_prefix()}SPD{self.speed_mrad_s.get()}", 500, "CyberGear 速度")

    def add_current_servo_position_action(self):
        self.add_action_row(f"{self.current_servo_prefix()}P{self.servo_pos_cmd.get()}", 1000, "PUL/DIR 伺服位置")

    def load_demo_actions(self):
        if self.action_running:
            messagebox.showwarning("提示", "动作循环运行中，先停止再导入示例")
            return
        self.clear_actions()
        demo_steps = [
            ("COMM:ALL", 300, "选择全部电机"),
            ("S1P100", 1200, "伺服1到100"),
            ("S1P0", 1200, "伺服1回零"),
            ("S2P100", 1200, "伺服2到100"),
            ("S2P0", 1200, "伺服2回零"),
            ("STOP", 1000, "总停"),
        ]
        for cmd, delay_ms, note in demo_steps:
            self.add_action_row(cmd, delay_ms, note)

    def get_action_steps(self, silent=False):
        steps = []
        for item in self.action_tree.get_children():
            values = self.action_tree.item(item, "values")
            if len(values) < 2:
                continue
            cmd = str(values[0]).strip()
            try:
                delay_ms = int(values[1])
            except Exception:
                delay_ms = 0
            note = str(values[2]).strip() if len(values) > 2 else ""
            if cmd:
                steps.append({"cmd": cmd, "delay_ms": max(0, delay_ms), "note": note})
        if not steps and not silent:
            messagebox.showwarning("提示", "请先添加至少一个动作步骤")
        return steps

    def get_action_repeat_count(self):
        try:
            repeat_count = int(str(self.action_repeat_var.get()).strip())
        except Exception:
            messagebox.showwarning("提示", "循环次数必须是整数，0 表示无限循环")
            return None
        if repeat_count < 0:
            messagebox.showwarning("提示", "循环次数不能小于 0")
            return None
        return repeat_count

    def start_action_loop(self):
        if self.action_running:
            messagebox.showinfo("提示", "动作循环已经在运行")
            return
        if not self.is_open:
            messagebox.showwarning("提示", "请先打开主串口")
            return
        steps = self.get_action_steps()
        if not steps:
            return
        repeat_count = self.get_action_repeat_count()
        if repeat_count is None:
            return
        self.action_stop_event.clear()
        self.action_running = True
        self.action_status_var.set("运行中")
        self.action_start_btn.config(state=tk.DISABLED)
        self.action_stop_btn.config(state=tk.NORMAL)
        loop_desc = "无限" if repeat_count == 0 else str(repeat_count)
        self.log(f"[动作循环] 开始：{len(steps)} 个步骤，循环 {loop_desc} 次\n")
        self.action_thread = threading.Thread(
            target=self.action_loop_worker,
            args=(steps, repeat_count),
            daemon=True
        )
        self.action_thread.start()

    def action_loop_worker(self, steps, repeat_count):
        stopped = False
        try:
            loop_index = 0
            while not self.action_stop_event.is_set() and (repeat_count == 0 or loop_index < repeat_count):
                loop_index += 1
                self.log(f"[动作循环] 第 {loop_index} 轮开始\n")
                for step_index, step in enumerate(steps, start=1):
                    if self.action_stop_event.is_set():
                        break
                    cmd = step["cmd"]
                    delay_ms = int(step.get("delay_ms", 0))
                    self.log(f"[动作循环] {step_index}/{len(steps)} 发送 {cmd}，等待 {delay_ms}ms\n")
                    self.send_main(cmd)
                    if delay_ms > 0 and self.action_stop_event.wait(delay_ms / 1000.0):
                        break
            stopped = self.action_stop_event.is_set()
        except Exception as e:
            self.log(f"[动作循环] 异常停止: {e}\n")
            stopped = True
        self.root.after(0, lambda: self.on_action_loop_finished(stopped))

    def on_action_loop_finished(self, stopped=False):
        self.action_running = False
        self.action_start_btn.config(state=tk.NORMAL)
        self.action_stop_btn.config(state=tk.DISABLED)
        self.action_status_var.set("已停止" if stopped else "完成")
        self.log("[动作循环] 已停止\n" if stopped else "[动作循环] 已完成\n")

    def stop_action_loop(self, send_stop=False):
        if self.action_running:
            self.action_stop_event.set()
            self.action_status_var.set("正在停止")
            self.log("[动作循环] 收到停止请求\n")
        if send_stop:
            self.send_main("STOP")

    def save_actions_to_file(self):
        steps = self.get_action_steps(silent=True)
        if not steps:
            messagebox.showwarning("提示", "没有可保存的动作步骤")
            return
        path = filedialog.asksaveasfilename(
            title="保存动作循环",
            defaultextension=".json",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")]
        )
        if not path:
            return
        data = {
            "repeat": self.action_repeat_var.get(),
            "steps": steps,
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self.log(f"[动作循环] 已保存到 {path}\n")
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    def load_actions_from_file(self):
        if self.action_running:
            messagebox.showwarning("提示", "动作循环运行中，先停止再载入")
            return
        path = filedialog.askopenfilename(
            title="载入动作循环",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")]
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            steps = data.get("steps", [])
            if not isinstance(steps, list):
                raise ValueError("文件格式错误：steps 不是列表")
            self.clear_actions()
            for step in steps:
                if not isinstance(step, dict):
                    continue
                self.add_action_row(step.get("cmd", ""), step.get("delay_ms", 0), step.get("note", ""))
            if "repeat" in data:
                self.action_repeat_var.set(str(data["repeat"]))
            self.log(f"[动作循环] 已载入 {path}\n")
        except Exception as e:
            messagebox.showerror("载入失败", str(e))


    def log(self, text):
        def write():
            self.log_text.insert(tk.END, text)
            self.log_text.see(tk.END)
        self.root.after(0, write)

    def refresh_port(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_cb["values"] = ports
        if ports and not self.port_cb.get():
            self.port_cb.current(0)

    def open_close_port(self):
        if not self.is_open:
            port = self.port_cb.get()
            if not port:
                messagebox.showwarning("提示", "请先选择主串口")
                return
            try:
                self.ser.port = port
                self.ser.baudrate = 9600
                self.ser.timeout = 0.1
                self.ser.parity = serial.PARITY_NONE
                self.ser.stopbits = serial.STOPBITS_ONE
                self.ser.bytesize = serial.EIGHTBITS
                self.ser.open()
                self.is_open = True
                self.open_btn.config(text="关闭串口")
                threading.Thread(target=self.read_main, daemon=True).start()
                self.log(f"[主串口] 已打开 {port}\n")
            except Exception as e:
                self.log(f"[主串口] 打开失败: {e}\n")
        else:
            try:
                self.ser.close()
            except Exception:
                pass
            self.is_open = False
            self.open_btn.config(text="打开串口")
            self.log("[主串口] 已关闭\n")

    def send_main(self, cmd):
        cmd = str(cmd).strip()
        if not cmd:
            return
        if not self.is_open:
            self.log("[主串口] 未打开，无法发送\n")
            return

        data = (cmd + "\n").encode("utf-8")
        try:
            with self.tx_lock:
                self.ser.write(data)
            self.log(f">> {cmd}\n")
        except Exception as e:
            self.log(f"[主串口] 发送失败: {e}\n")

    def send_from_entry(self):
        cmd = self.cmd_var.get().strip()
        self.send_main(cmd)

    def read_main(self):
        while self.is_open:
            try:
                data = self.ser.read(self.ser.in_waiting or 1)
                if data:
                    text = data.decode("utf-8", errors="replace")
                    self.log(text)
            except Exception as e:
                self.log(f"[主串口] 接收错误: {e}\n")
                break
            time.sleep(0.01)

    def send_comm_select(self):
        self.send_main(f"COMM:{self.comm_var.get()}")

    def current_motor_prefix(self):
        return f"M{self.motor_id.get()}"

    def quick_select_enable(self):
        mid = self.motor_id.get()
        self.comm_var.set(f"CAN{mid}")
        self.send_main(f"COMM:CAN{mid}")
        self.root.after(120, lambda: self.send_main(f"M{mid}E"))

    def send_enable(self):
        self.send_main(f"{self.current_motor_prefix()}E")

    def send_stop_motor(self):
        self.send_main(f"{self.current_motor_prefix()}STOP")

    def send_zero(self):
        self.send_main(f"{self.current_motor_prefix()}Z")

    def send_status(self):
        self.send_main(f"{self.current_motor_prefix()}?")

    def send_mode(self, mode):
        self.send_main(f"{self.current_motor_prefix()}MODE{mode}")

    def send_native_position(self):
        self.send_main(f"{self.current_motor_prefix()}POS{self.pos_mrad.get()},{self.pos_speed_mrad_s.get()}")

    def send_native_speed(self):
        self.send_main(f"{self.current_motor_prefix()}SPD{self.speed_mrad_s.get()}")

    def send_current(self):
        self.send_main(f"{self.current_motor_prefix()}IQ{self.iq_ma.get()}")

    def send_limit(self):
        self.send_main(f"{self.current_motor_prefix()}L{self.limit_min.get()},{self.limit_max.get()}")

    def send_set_id(self):
        self.send_main(f"SETID{self.old_id_var.get()},{self.new_id_var.get()}")

    def quick_set_id(self, old_id, new_id):
        self.old_id_var.set(old_id)
        self.new_id_var.set(new_id)
        self.send_main(f"SETID{old_id},{new_id}")

    def quick_pos(self, mid, pos, spd):
        self.motor_id.set(mid)
        self.pos_mrad.set(pos)
        self.pos_speed_mrad_s.set(spd)
        self.send_main(f"M{mid}POS{pos},{spd}")

    def quick_speed(self, mid, spd):
        self.motor_id.set(mid)
        self.speed_mrad_s.set(spd)
        self.send_main(f"M{mid}SPD{spd}")

    def quick_limit(self, mid, mn, mx):
        self.motor_id.set(mid)
        self.limit_min.set(mn)
        self.limit_max.set(mx)
        self.send_main(f"M{mid}L{mn},{mx}")

    def current_servo_prefix(self):
        return f"S{self.servo_id.get()}"

    def send_servo_speed(self):
        self.send_main(f"{self.current_servo_prefix()}S{self.servo_speed_us.get()}")

    def send_servo_position(self):
        self.send_main(f"{self.current_servo_prefix()}P{self.servo_pos_cmd.get()}")

    def send_servo_zero(self):
        self.send_main(f"{self.current_servo_prefix()}Z")

    def send_servo_stop(self):
        self.send_main(f"{self.current_servo_prefix()}STOP")

    def quick_servo_pos(self, servo_id, pos):
        self.servo_id.set(servo_id)
        self.servo_pos_cmd.set(pos)
        self.send_main(f"S{servo_id}P{pos}")


    # ==================== 第二页：乒乓球发球机 + 语音 ====================

    def build_pingpong_page(self, parent):
        self.tt_topspin_var = tk.IntVar(value=0)   # 正数上旋，负数下旋
        self.tt_sidespin_var = tk.IntVar(value=0)  # 正数右侧旋，负数左侧旋
        self.tt_power_var = tk.IntVar(value=50)    # 0~100

        page = ttk.Frame(parent, padding=8)
        page.pack(fill="both", expand=True)

        top_bar = ttk.Frame(page)
        top_bar.pack(fill="x", pady=(0, 6))

        ttk.Label(top_bar, text="乒乓球发球机", font=("Microsoft YaHei UI", 18, "bold")).pack(side=tk.LEFT, padx=4)
        ttk.Button(top_bar, text="返回第一页", command=lambda: self.notebook.select(self.page_original)).pack(side=tk.RIGHT, padx=4)

        body = ttk.PanedWindow(page, orient=tk.HORIZONTAL)
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body)
        right = ttk.Frame(body)
        body.add(left, weight=2)
        body.add(right, weight=3)

        param_frame = ttk.LabelFrame(left, text="发球参数", padding=8)
        param_frame.pack(fill="x", padx=4, pady=5)

        self._build_tt_scale(param_frame, "上旋 / 下旋", self.tt_topspin_var, -100, 100, 0)
        self._build_tt_scale(param_frame, "左侧旋 / 右侧旋", self.tt_sidespin_var, -100, 100, 1)
        self._build_tt_scale(param_frame, "力度", self.tt_power_var, 0, 100, 2)

        self.tt_param_label = ttk.Label(param_frame, text="", foreground="#005a9e")
        self.tt_param_label.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 2))

        btn_frame = ttk.LabelFrame(left, text="发球控制 / 串口指令", padding=8)
        btn_frame.pack(fill="x", padx=4, pady=5)

        ttk.Button(btn_frame, text="发送参数 TTSET", command=self.tt_send_params).grid(row=0, column=0, padx=4, pady=4, sticky="ew")
        ttk.Button(btn_frame, text="单次发球 TTSHOT", command=self.tt_shot).grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        ttk.Button(btn_frame, text="停止发球 TTSTOP", command=self.tt_stop).grid(row=1, column=0, padx=4, pady=4, sticky="ew")
        ttk.Button(btn_frame, text="急停 STOP", command=self.tt_emergency_stop).grid(row=1, column=1, padx=4, pady=4, sticky="ew")
        btn_frame.columnconfigure(0, weight=1)
        btn_frame.columnconfigure(1, weight=1)

        voice_frame = ttk.LabelFrame(left, text="语音识别 / 播放声音", padding=8)
        voice_frame.pack(fill="both", expand=True, padx=4, pady=5)

        ttk.Label(voice_frame, text="讯飞 APPID:").grid(row=0, column=0, sticky="e", padx=3, pady=3)
        self.xf_appid_var = tk.StringVar(value="")
        ttk.Entry(voice_frame, textvariable=self.xf_appid_var, width=32).grid(row=0, column=1, columnspan=2, sticky="ew", padx=3, pady=3)

        ttk.Label(voice_frame, text="API_KEY:").grid(row=1, column=0, sticky="e", padx=3, pady=3)
        self.xf_api_key_var = tk.StringVar(value="")
        ttk.Entry(voice_frame, textvariable=self.xf_api_key_var, width=32).grid(row=1, column=1, columnspan=2, sticky="ew", padx=3, pady=3)

        ttk.Label(voice_frame, text="API_SECRET:").grid(row=2, column=0, sticky="e", padx=3, pady=3)
        self.xf_api_secret_var = tk.StringVar(value="")
        ttk.Entry(voice_frame, textvariable=self.xf_api_secret_var, width=32, show="*").grid(row=2, column=1, columnspan=2, sticky="ew", padx=3, pady=3)

        self.voice_status_var = tk.StringVar(value="未启动。没有 Key 时可用下面的模拟语音输入先测试。")
        ttk.Label(voice_frame, textvariable=self.voice_status_var, foreground="#8a4b00", wraplength=420).grid(
            row=3, column=0, columnspan=3, sticky="w", padx=3, pady=3
        )

        self.voice_result_var = tk.StringVar(value="识别结果：")
        ttk.Label(voice_frame, textvariable=self.voice_result_var, foreground="#005a9e", wraplength=420).grid(
            row=4, column=0, columnspan=3, sticky="w", padx=3, pady=4
        )

        self.voice_start_btn = ttk.Button(voice_frame, text="开始语音识别", command=self.start_voice_recognition)
        self.voice_start_btn.grid(row=5, column=0, padx=3, pady=5, sticky="ew")
        self.voice_stop_btn = ttk.Button(voice_frame, text="停止语音识别", command=self.stop_voice_recognition, state=tk.DISABLED)
        self.voice_stop_btn.grid(row=5, column=1, padx=3, pady=5, sticky="ew")
        ttk.Button(voice_frame, text="测试播报", command=lambda: self.speak("语音播报正常")).grid(row=5, column=2, padx=3, pady=5, sticky="ew")

        ttk.Label(voice_frame, text="模拟语音:").grid(row=6, column=0, sticky="e", padx=3, pady=3)
        self.voice_manual_var = tk.StringVar(value="上旋六十右侧旋二十力度七十开始发球")
        manual_entry = ttk.Entry(voice_frame, textvariable=self.voice_manual_var)
        manual_entry.grid(row=6, column=1, sticky="ew", padx=3, pady=3)
        manual_entry.bind("<Return>", lambda event: self.handle_voice_text(self.voice_manual_var.get()))
        ttk.Button(voice_frame, text="解析", command=lambda: self.handle_voice_text(self.voice_manual_var.get())).grid(row=6, column=2, padx=3, pady=3, sticky="ew")

        ttk.Label(
            voice_frame,
            text="口令示例：上旋60；下旋40；左侧旋30；力度70；开始发球；单次发球；停止发球；急停。",
            wraplength=420
        ).grid(row=7, column=0, columnspan=3, sticky="w", padx=3, pady=5)

        voice_frame.columnconfigure(1, weight=1)

        table_frame = ttk.LabelFrame(right, text="虚拟乒乓球桌 / 轨迹预览", padding=8)
        table_frame.pack(fill="both", expand=True, padx=4, pady=5)

        self.tt_canvas = tk.Canvas(table_frame, bg="#f3f5f7", height=560, highlightthickness=0)
        self.tt_canvas.pack(fill="both", expand=True)
        self.tt_canvas.bind("<Configure>", lambda event: self.draw_pingpong_table())

        self.update_pingpong_preview()

    def _build_tt_scale(self, parent, title, var, from_, to_, row):
        ttk.Label(parent, text=title).grid(row=row, column=0, sticky="w", padx=3, pady=4)
        scale = tk.Scale(
            parent,
            from_=from_,
            to=to_,
            orient=tk.HORIZONTAL,
            variable=var,
            resolution=1,
            showvalue=True,
            length=260,
            command=lambda value: self.update_pingpong_preview()
        )
        scale.grid(row=row, column=1, sticky="ew", padx=3, pady=4)
        spin = ttk.Spinbox(parent, from_=from_, to=to_, textvariable=var, width=8, command=self.update_pingpong_preview)
        spin.grid(row=row, column=2, sticky="e", padx=3, pady=4)
        parent.columnconfigure(1, weight=1)

    def update_pingpong_preview(self):
        try:
            topspin = int(self.tt_topspin_var.get())
            sidespin = int(self.tt_sidespin_var.get())
            power = int(self.tt_power_var.get())
        except Exception:
            return

        topspin = max(-100, min(100, topspin))
        sidespin = max(-100, min(100, sidespin))
        power = max(0, min(100, power))

        self.tt_topspin_var.set(topspin)
        self.tt_sidespin_var.set(sidespin)
        self.tt_power_var.set(power)

        if topspin > 0:
            spin_text = f"上旋 {topspin}"
        elif topspin < 0:
            spin_text = f"下旋 {-topspin}"
        else:
            spin_text = "无上/下旋"

        if sidespin > 0:
            side_text = f"右侧旋 {sidespin}"
        elif sidespin < 0:
            side_text = f"左侧旋 {-sidespin}"
        else:
            side_text = "无侧旋"

        self.tt_param_label.config(text=f"{spin_text}  |  {side_text}  |  力度 {power}%  |  指令：{self.tt_make_cmd()}")
        self.draw_pingpong_table()

    def draw_pingpong_table(self):
        if not hasattr(self, "tt_canvas"):
            return

        c = self.tt_canvas
        c.delete("all")

        w = max(c.winfo_width(), 500)
        h = max(c.winfo_height(), 360)

        margin = 44
        x0 = margin
        y0 = margin
        x1 = w - margin
        y1 = h - margin

        c.create_rectangle(x0 + 8, y0 + 8, x1 + 8, y1 + 8, fill="#c9ced6", outline="")
        c.create_rectangle(x0, y0, x1, y1, fill="#1f8a5b", outline="#174f3c", width=3)
        c.create_rectangle(x0 + 10, y0 + 10, x1 - 10, y1 - 10, outline="#f4f4f4", width=3)

        net_y = (y0 + y1) / 2
        c.create_line(x0 + 6, net_y, x1 - 6, net_y, fill="#eeeeee", width=5)
        for xx in range(int(x0), int(x1), 18):
            c.create_line(xx, net_y - 7, xx + 9, net_y + 7, fill="#333333", width=1)

        c.create_line((x0 + x1) / 2, y0 + 10, (x0 + x1) / 2, y1 - 10, fill="#f4f4f4", width=2)

        topspin = int(self.tt_topspin_var.get())
        sidespin = int(self.tt_sidespin_var.get())
        power = int(self.tt_power_var.get())

        sx = (x0 + x1) / 2
        sy = y1 - 34

        travel = (y1 - y0) * (0.30 + power / 140.0)
        ey = sy - travel
        ey = max(y0 + 25, min(y1 - 30, ey))

        ex = sx + sidespin * (x1 - x0) / 230.0
        ex = max(x0 + 25, min(x1 - 25, ex))

        cx = (sx + ex) / 2 + sidespin * 0.75
        cy = (sy + ey) / 2 - topspin * 0.45

        pts = []
        for i in range(70):
            t = i / 69.0
            x = (1 - t) * (1 - t) * sx + 2 * (1 - t) * t * cx + t * t * ex
            y = (1 - t) * (1 - t) * sy + 2 * (1 - t) * t * cy + t * t * ey
            pts.extend([x, y])

        c.create_line(*pts, fill="#ffffff", width=8, smooth=True)
        c.create_line(*pts, fill="#d72828", width=4, smooth=True)

        c.create_oval(sx - 10, sy - 10, sx + 10, sy + 10, fill="#ffd52e", outline="#4d3b00", width=2)
        c.create_oval(ex - 9, ey - 9, ex + 9, ey + 9, fill="#ffffff", outline="#111111", width=2)
        c.create_oval(ex - 18, ey - 18, ex + 18, ey + 18, outline="#ffffff", width=2)

        c.create_text(x0 + 16, y1 - 18, text="发球点", fill="#ffffff", anchor="w")
        c.create_text(ex + 14, ey - 14, text="落点", fill="#ffffff", anchor="w")
        c.create_text(20, 20, text=self.tt_canvas_text(), fill="#222222", anchor="w", font=("Arial", 11))

    def tt_canvas_text(self):
        topspin = int(self.tt_topspin_var.get())
        sidespin = int(self.tt_sidespin_var.get())
        power = int(self.tt_power_var.get())

        if topspin > 0:
            spin = f"Topspin {topspin}"
        elif topspin < 0:
            spin = f"Backspin {-topspin}"
        else:
            spin = "No top/back spin"

        if sidespin > 0:
            side = f"Right sidespin {sidespin}"
        elif sidespin < 0:
            side = f"Left sidespin {-sidespin}"
        else:
            side = "No sidespin"

        return f"{spin} | {side} | Power {power}%"

    def tt_make_cmd(self):
        return f"TTSET,{int(self.tt_topspin_var.get())},{int(self.tt_sidespin_var.get())},{int(self.tt_power_var.get())}"

    def tt_send_params(self):
        self.send_main(self.tt_make_cmd())
        self.speak("参数已发送")

    def tt_shot(self):
        self.send_main(self.tt_make_cmd())
        self.root.after(120, lambda: self.send_main("TTSHOT"))
        self.speak("单次发球")

    def tt_stop(self):
        self.send_main("TTSTOP")
        self.speak("已停止发球")

    def tt_emergency_stop(self):
        self.send_main("STOP")
        self.root.after(120, lambda: self.send_main("TTSTOP"))
        self.speak("已急停")

    def speak(self, text):
        text = str(text).strip()
        if not text:
            return

        if self.speaker_engine is None:
            try:
                self.root.bell()
            except Exception:
                pass
            self.log("[语音播报] 未安装或无法初始化 pyttsx3，已使用系统提示音\n")
            return

        def worker():
            with self.speaker_lock:
                try:
                    self.speaker_engine.say(text)
                    self.speaker_engine.runAndWait()
                except Exception as e:
                    self.log(f"[语音播报] 失败: {e}\n")

        threading.Thread(target=worker, daemon=True).start()

    def clean_voice_text(self, text):
        text = str(text).strip()
        for ch in [" ", "，", ",", "。", ".", "！", "!", "？", "?"]:
            text = text.replace(ch, "")
        text = text.replace("百分之", "")
        return text

    def cn_number_to_int(self, text):
        text = str(text).strip()
        if not text:
            return None
        if text.isdigit():
            return int(text)

        mp = {
            "零": 0, "〇": 0, "一": 1, "幺": 1, "二": 2, "两": 2,
            "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
            "八": 8, "九": 9,
        }

        if text == "百" or text == "一百":
            return 100

        if "百" in text:
            a, _, b = text.partition("百")
            hundred = mp.get(a, 1) if a else 1
            rest = self.cn_number_to_int(b) if b else 0
            return hundred * 100 + (rest or 0)

        if "十" in text:
            a, _, b = text.partition("十")
            tens = mp.get(a, 1) if a else 1
            ones = mp.get(b, 0) if b else 0
            return tens * 10 + ones

        return mp.get(text)

    def extract_number(self, text):
        m = re.search(r"\d+", text)
        if m:
            return int(m.group(0))
        m = re.search(r"[零〇一幺二两三四五六七八九十百]+", text)
        if m:
            return self.cn_number_to_int(m.group(0))
        return None

    def number_after_keyword(self, text, keyword):
        if keyword not in text:
            return None
        part = text[text.find(keyword) + len(keyword): text.find(keyword) + len(keyword) + 8]
        m = re.search(r"\d+", part)
        if m:
            return int(m.group(0))
        m = re.search(r"[零〇一幺二两三四五六七八九十百]+", part)
        if m:
            return self.cn_number_to_int(m.group(0))
        return None

    def handle_voice_text(self, raw_text):
        text = self.clean_voice_text(raw_text)
        if not text:
            return

        self.voice_result_var.set("识别结果：" + text)
        self.log(f"[语音识别] {text}\n")

        if any(k in text for k in ["急停", "紧急停止", "全部停止", "立刻停止"]):
            self.tt_emergency_stop()
            return

        if any(k in text for k in ["停止发球", "停止出球", "停止打球"]):
            self.tt_stop()
            return

        changed = False

        if "上旋" in text:
            v = self.number_after_keyword(text, "上旋")
            if v is None:
                v = self.extract_number(text)
            if v is None:
                v = 60
            self.tt_topspin_var.set(max(0, min(100, v)))
            changed = True

        if "下旋" in text:
            v = self.number_after_keyword(text, "下旋")
            if v is None:
                v = self.extract_number(text)
            if v is None:
                v = 60
            self.tt_topspin_var.set(-max(0, min(100, v)))
            changed = True

        if "左侧旋" in text or "左旋" in text:
            kw = "左侧旋" if "左侧旋" in text else "左旋"
            v = self.number_after_keyword(text, kw)
            if v is None:
                v = self.extract_number(text)
            if v is None:
                v = 40
            self.tt_sidespin_var.set(-max(0, min(100, v)))
            changed = True

        if "右侧旋" in text or "右旋" in text:
            kw = "右侧旋" if "右侧旋" in text else "右旋"
            v = self.number_after_keyword(text, kw)
            if v is None:
                v = self.extract_number(text)
            if v is None:
                v = 40
            self.tt_sidespin_var.set(max(0, min(100, v)))
            changed = True

        if any(k in text for k in ["无旋", "不转", "不要旋转"]):
            self.tt_topspin_var.set(0)
            changed = True

        if any(k in text for k in ["无侧旋", "不要侧旋"]):
            self.tt_sidespin_var.set(0)
            changed = True

        if any(k in text for k in ["力度", "力量", "速度", "球速"]):
            kw = "力度"
            for candidate in ["力度", "力量", "速度", "球速"]:
                if candidate in text:
                    kw = candidate
                    break
            v = self.number_after_keyword(text, kw)
            if v is None:
                v = self.extract_number(text)
            if v is None:
                v = 70
            self.tt_power_var.set(max(0, min(100, v)))
            changed = True

        if any(k in text for k in ["大一点", "快一点", "增加力度", "力度增加"]):
            self.tt_power_var.set(max(0, min(100, int(self.tt_power_var.get()) + 10)))
            changed = True

        if any(k in text for k in ["小一点", "慢一点", "减少力度", "力度减少"]):
            self.tt_power_var.set(max(0, min(100, int(self.tt_power_var.get()) - 10)))
            changed = True

        if changed:
            self.update_pingpong_preview()
            self.send_main(self.tt_make_cmd())
            self.speak("参数已设置")

        if any(k in text for k in ["开始发球", "开始出球", "启动发球"]):
            self.tt_shot()
            return

        if any(k in text for k in ["单次发球", "发一个", "发一球", "来一个", "来一球"]):
            self.tt_shot()
            return

        if not changed:
            self.speak("没有识别到有效指令")

    def start_voice_recognition(self):
        appid = self.xf_appid_var.get().strip()
        api_key = self.xf_api_key_var.get().strip()
        api_secret = self.xf_api_secret_var.get().strip()

        if not appid or not api_key or not api_secret:
            msg = "未填写完整讯飞 APPID / API_KEY / API_SECRET，已取消连接，避免 401 刷屏。"
            self.voice_status_var.set(msg)
            self.log("[语音识别] " + msg + "\n")
            messagebox.showwarning("讯飞参数缺失", msg)
            return

        if websocket is None:
            messagebox.showwarning("缺少依赖", "未安装 websocket-client，请执行：pip install websocket-client")
            return

        if shutil.which("arecord") is None:
            messagebox.showwarning(
                "缺少依赖",
                "未找到 arecord。请先安装 ALSA 工具：\n\nsudo apt install -y alsa-utils"
            )
            return

        if self.voice_running:
            self.log("[语音识别] 已经在运行\n")
            return

        self.voice_stop_event.clear()
        self.voice_text_parts = []
        self.voice_running = True
        self.voice_start_btn.config(state=tk.DISABLED)
        self.voice_stop_btn.config(state=tk.NORMAL)
        self.voice_status_var.set("正在连接讯飞中英识别大模型...")
        self.log("[语音识别] 正在连接讯飞中英识别大模型...\n")

        self.voice_thread = threading.Thread(
            target=self.xunfei_iat_worker,
            args=(appid, api_key, api_secret),
            daemon=True
        )
        self.voice_thread.start()

    def stop_voice_recognition(self):
        self.voice_stop_event.set()
        try:
            if self.voice_ws is not None:
                self.voice_ws.close()
        except Exception:
            pass
        self.voice_running = False
        self.voice_start_btn.config(state=tk.NORMAL)
        self.voice_stop_btn.config(state=tk.DISABLED)
        self.voice_status_var.set("语音识别已停止")
        self.log("[语音识别] 已停止\n")

    def xunfei_create_iat_url(self, api_key, api_secret):
        host = XUNFEI_IAT_HOST
        path = XUNFEI_IAT_PATH
        date = formatdate(timeval=None, localtime=False, usegmt=True)

        signature_origin = f"host: {host}\ndate: {date}\nGET {path} HTTP/1.1"
        signature_sha = hmac.new(
            api_secret.encode("utf-8"),
            signature_origin.encode("utf-8"),
            digestmod=hashlib.sha256
        ).digest()
        signature = base64.b64encode(signature_sha).decode("utf-8")

        authorization_origin = (
            f'api_key="{api_key}", '
            f'algorithm="hmac-sha256", '
            f'headers="host date request-line", '
            f'signature="{signature}"'
        )
        authorization = base64.b64encode(authorization_origin.encode("utf-8")).decode("utf-8")

        params = {
            "authorization": authorization,
            "date": date,
            "host": host,
        }
        return f"wss://{host}{path}?{urlencode(params)}"

    def xunfei_iat_worker(self, appid, api_key, api_secret):
        def ui_status(msg):
            self.root.after(0, lambda: self.voice_status_var.set(msg))
            self.log("[语音识别] " + msg + "\n")

        def on_open(ws):
            ui_status("连接成功，开始采集麦克风。说完后可点击停止。")
            threading.Thread(target=self.xunfei_send_audio, args=(ws, appid), daemon=True).start()

        def on_message(ws, message):
            try:
                data = json.loads(message)

                header = data.get("header", {})
                code = header.get("code", 0)
                if code != 0:
                    ui_status(f"接口返回错误：{data}")
                    return

                piece = self.xunfei_decode_bigmodel_result(data)

                if piece:
                    self.voice_text_parts.append(piece)
                    current = "".join(self.voice_text_parts)
                    self.root.after(0, lambda t=current: self.voice_result_var.set("识别结果：" + t))

                header_status = header.get("status")
                payload_status = data.get("payload", {}).get("result", {}).get("status")
                if header_status == 2 or payload_status == 2:
                    final = "".join(self.voice_text_parts).strip()
                    if final:
                        self.root.after(0, lambda t=final: self.handle_voice_text(t))
                    ui_status("本轮识别完成")
            except Exception as e:
                ui_status(f"消息解析失败：{e}")

        def on_error(ws, error):
            err = str(error)
            ui_status("连接错误：" + err)
            if "401" in err or "Unauthorized" in err or "apikey not found" in err:
                self.root.after(0, lambda: messagebox.showerror(
                    "讯飞鉴权失败",
                    "401 Unauthorized：API_KEY 没通过。\n\n"
                    "请确认：\n"
                    "1. 用的是“中英识别大模型”的 APPID/API_KEY/API_SECRET；\n"
                    "2. 请求地址必须是 wss://iat.xf-yun.com/v1；\n"
                    "3. Key 没有多复制空格，APIKey/APISecret 不要填反；\n"
                    "4. 电脑系统时间要准确，和 GMT 时间偏差不要太大。"
                ))

        def on_close(ws, close_status_code, close_msg):
            ui_status("连接已关闭")
            self.root.after(0, self._voice_closed_ui)

        try:
            url = self.xunfei_create_iat_url(api_key, api_secret)
            self.voice_ws = websocket.WebSocketApp(
                url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            self.voice_ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
        except Exception as e:
            ui_status(f"启动失败：{e}")
            self.root.after(0, self._voice_closed_ui)

    def _voice_closed_ui(self):
        self.voice_running = False
        try:
            self.voice_start_btn.config(state=tk.NORMAL)
            self.voice_stop_btn.config(state=tk.DISABLED)
        except Exception:
            pass

    def xunfei_decode_bigmodel_result(self, data):
        """
        讯飞“中英识别大模型”返回的 payload.result.text 是 base64 编码后的 JSON。
        这里把它解码成普通文字。
        """
        try:
            result = data.get("payload", {}).get("result", {})
            text_b64 = result.get("text", "")
            if not text_b64:
                return ""

            decoded = base64.b64decode(text_b64).decode("utf-8", errors="ignore")
            obj = json.loads(decoded)

            piece = ""
            for item in obj.get("ws", []):
                for cw in item.get("cw", []):
                    piece += cw.get("w", "")
            return piece
        except Exception as e:
            self.log(f"[语音识别] 结果解码失败：{e}\n")
            return ""

    def xunfei_send_audio(self, ws, appid):
        """
        不使用 PyAudio，直接调用 Linux/Ubuntu 的 arecord 采集麦克风。
        输出格式：
            16kHz / 16bit / mono / raw PCM
        依赖：
            sudo apt install -y alsa-utils
        """
        proc = None
        try:
            cmd = [
                "arecord",
                "-q",
                "-D", "default",
                "-f", "S16_LE",
                "-r", "16000",
                "-c", "1",
                "-t", "raw"
            ]

            self.log("[语音识别] 使用 arecord 采集麦克风：16kHz 16bit mono raw PCM\\n")

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0
            )

            seq = 1
            first_frame = True
            chunk_size = 1280

            while not self.voice_stop_event.is_set():
                audio = proc.stdout.read(chunk_size)
                if not audio:
                    if proc.poll() is not None:
                        err = proc.stderr.read().decode("utf-8", errors="ignore")
                        self.log("[语音识别] arecord 已退出：" + err + "\\n")
                        break
                    time.sleep(0.02)
                    continue

                audio_b64 = base64.b64encode(audio).decode("utf-8")

                if first_frame:
                    payload = {
                        "header": {
                            "app_id": appid,
                            "status": 0
                        },
                        "parameter": {
                            "iat": {
                                "domain": "slm",
                                "language": "zh_cn",
                                "accent": "mandarin",
                                "eos": 1200,
                                "result": {
                                    "encoding": "utf8",
                                    "compress": "raw",
                                    "format": "json"
                                }
                            }
                        },
                        "payload": {
                            "audio": {
                                "encoding": "raw",
                                "sample_rate": 16000,
                                "channels": 1,
                                "bit_depth": 16,
                                "seq": seq,
                                "status": 0,
                                "audio": audio_b64
                            }
                        }
                    }
                    first_frame = False
                else:
                    payload = {
                        "header": {
                            "app_id": appid,
                            "status": 1
                        },
                        "payload": {
                            "audio": {
                                "encoding": "raw",
                                "sample_rate": 16000,
                                "channels": 1,
                                "bit_depth": 16,
                                "seq": seq,
                                "status": 1,
                                "audio": audio_b64
                            }
                        }
                    }

                ws.send(json.dumps(payload, ensure_ascii=False))
                seq += 1
                time.sleep(0.04)

            end_payload = {
                "header": {
                    "app_id": appid,
                    "status": 2
                },
                "payload": {
                    "audio": {
                        "encoding": "raw",
                        "sample_rate": 16000,
                        "channels": 1,
                        "bit_depth": 16,
                        "seq": seq,
                        "status": 2,
                        "audio": ""
                    }
                }
            }
            ws.send(json.dumps(end_payload, ensure_ascii=False))
            time.sleep(0.2)
            ws.close()

        except Exception as e:
            self.log(f"[语音识别] arecord 采集/发送失败：{e}\\n")
            try:
                ws.close()
            except Exception:
                pass
        finally:
            try:
                if proc is not None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=1)
                    except Exception:
                        proc.kill()
            except Exception:
                pass



if __name__ == "__main__":
    root = tk.Tk()
    app = CyberGear2ServoGUI(root)
    root.mainloop()
