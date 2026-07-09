import serial
import serial.tools.list_ports
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading
import time
import json


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

        self.setup_ui()

        try:
            self.root.state("zoomed")
        except Exception:
            pass

    def setup_ui(self):
        main = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
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


if __name__ == "__main__":
    root = tk.Tk()
    app = CyberGear2ServoGUI(root)
    root.mainloop()
