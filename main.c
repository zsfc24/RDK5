#include "stm32f10x.h"
#include "string.h"
#include "stdlib.h"
#include "stdio.h"
#include "stdint.h"

/*
 * STM32F103 + 2 路 PUL/DIR 伺服 + 2 个 Xiaomi CyberGear 关节电机
 *
 * 串口：USART1 PA9 TX / PA10 RX，9600
 * CAN： CAN1 PA11 RX / PA12 TX，1Mbps，扩展帧
 *
 * 伺服 1：PUL = PA8  / TIM1_CH1，DIR = PB12
 * 伺服 2：PUL = PB6  / TIM4_CH1，DIR = PB13
 *
 * 为了不和小米关节电机原来的 M1/M2 指令冲突：
 *   - CyberGear 仍然使用 M1... / M2... 原来的控制方式
 *   - 两个 PUL/DIR 伺服使用 S1... / S2... 指令
 *
 * 伺服指令：
 *   S1P100       伺服1走绝对位置，位置比例：10个指令单位 = 1mm
 *   S2P-50       伺服2走绝对位置
 *   S1S500       设置伺服1脉冲周期，单位 us，越小越快
 *   S2S500       设置伺服2脉冲周期
 *   S1Z / S2Z    当前位置设零
 *   S1STOP       停止伺服1
 *   S2STOP       停止伺服2
 *   SSTOP        停止两个伺服
 *
 * CyberGear 指令保持原先风格：
 *   SCAN / CAN? / COMM:MAIN / COMM:CAN1 / COMM:CAN2 / COMM:ALL / COMM?
 *   M1E / M2E / M1STOP / M2STOP / M1Z / M2Z / STOP / CLEAR
 *   M1POS1000,444 / M2POS1000,444
 *   M1SPD1000 / M2SPD-1000 / M1MODE0~3 / M1IQ1000
 *   M1Cpos,vel,kp_x10,kd_x100,tq_mNm
 *   M1L-3000,3000 / M2L-3000,3000 / LIMIT?
 */

#define UART_BUF_LEN        128

#define SERVO_PULSE_PER_MM  3200
#define SERVO_DEFAULT_US    500
#define SERVO_MIN_US        20
#define SERVO_MAX_US        60000

#define CYBER_MASTER_ID     0x00
#define CYBER_M1_ID         0x01
#define CYBER_M2_ID         0x02
#define CYBER_SCAN_MAX_ID   127

/* 运控模式范围：pos mrad，vel mrad/s，torque mNm，kp x10，kd x100 */
#define POS_MIN_MRAD        (-12566)
#define POS_MAX_MRAD        (12566)
#define VEL_MIN_MRAD_S      (-30000)
#define VEL_MAX_MRAD_S      (30000)
#define TQ_MIN_MNM          (-12000)
#define TQ_MAX_MNM          (12000)
#define KP_MIN_X10          0
#define KP_MAX_X10          5000
#define KD_MIN_X100         0
#define KD_MAX_X100         500

#define DEFAULT_KP_X10      200
#define DEFAULT_KD_X100     50
#define DEFAULT_TQ_MNM      0

/* CyberGear 原生模式参数 index */
#define CYBER_INDEX_RUN_MODE     0x7005
#define CYBER_INDEX_IQ_REF       0x7006
#define CYBER_INDEX_SPD_REF      0x700A
#define CYBER_INDEX_LIMIT_TQ     0x700B
#define CYBER_INDEX_LOC_REF      0x7016
#define CYBER_INDEX_LIMIT_SPD    0x7017
#define CYBER_INDEX_LIMIT_CUR    0x7018

#define CYBER_RUN_MODE_MIT       0
#define CYBER_RUN_MODE_POS       1
#define CYBER_RUN_MODE_SPEED     2
#define CYBER_RUN_MODE_CURRENT   3

#define DEFAULT_LIMIT_CUR_A      10.0f

#define M1_LIMIT_MIN_MRAD        (-3000)
#define M1_LIMIT_MAX_MRAD        (3000)
#define M2_LIMIT_MIN_MRAD        (-3000)
#define M2_LIMIT_MAX_MRAD        (3000)

volatile uint8_t uart_buf[UART_BUF_LEN];
volatile uint8_t uart_cnt = 0;
volatile uint8_t recv_end = 0;

typedef enum {
    COMM_MAIN = 0,
    COMM_CAN1,
    COMM_CAN2,
    COMM_ALL
} CommSelect_t;

volatile CommSelect_t comm_select = COMM_MAIN;

typedef struct {
    int32_t target_pos;
    int32_t current_pos;
    int32_t pulse_remain;
    uint8_t dir;
    uint8_t is_running;
    uint16_t speed_us;
} ServoMotor_t;

volatile ServoMotor_t servo1 = {0, 0, 0, 0, 0, SERVO_DEFAULT_US};
volatile ServoMotor_t servo2 = {0, 0, 0, 0, 0, SERVO_DEFAULT_US};

typedef struct {
    uint8_t id;
    int32_t pos_mrad;
    int32_t vel_mrad_s;
    int32_t torque_mNm;
    int16_t temp_x10;
    uint8_t mode;
    uint8_t fault;
    volatile uint8_t feedback_pending;
} CyberMotor_t;

CyberMotor_t cyber1 = {CYBER_M1_ID, 0, 0, 0, 0, 0, 0, 0};
CyberMotor_t cyber2 = {CYBER_M2_ID, 0, 0, 0, 0, 0, 0, 0};

typedef struct {
    uint8_t enabled;
    int32_t min_mrad;
    int32_t max_mrad;
} CyberAngleLimit_t;

CyberAngleLimit_t limit1 = {1, M1_LIMIT_MIN_MRAD, M1_LIMIT_MAX_MRAD};
CyberAngleLimit_t limit2 = {1, M2_LIMIT_MIN_MRAD, M2_LIMIT_MAX_MRAD};

volatile uint8_t cyber_scan_found[128];

/* 基础初始化 */
void NVIC_Config_Init(void);
void My_GPIO_Init(void);
void UART1_Init(void);
void CAN1_Init_1Mbps_PA11_PA12(void);
void Servo_TIM1_Init(void);
void Servo_TIM4_Init(void);

/* UART */
void UART_SendByte(uint8_t ch);
void UART_SendStr(const char *str);
void Parse_Command(char* buf);
void Delay_Cycles(volatile uint32_t n);

/* 通用 */
int32_t PosCmd_ToPulse(int pos);
int32_t I32_Abs(int32_t x);
int32_t Limit_Int32(int32_t x, int32_t min, int32_t max);
void Trim_CRLF(char *s);

/* 伺服 */
void Servo_SetSpeed(uint8_t servo_id, uint16_t speed_us);
void Servo_RunAbs(uint8_t servo_id, int pos_cmd);
void Servo_SetZero(uint8_t servo_id);
void Servo_Stop(uint8_t servo_id);
void Servo_StopAll(void);
void Servo_StartPWM(uint8_t servo_id);
void Servo_StopPWM(uint8_t servo_id);
void Servo_SetDir(uint8_t servo_id, uint8_t dir);
void Servo_Report(uint8_t servo_id, const char *msg);

/* CyberGear */
void Send_Comm_Select_Report(void);
uint8_t Comm_AllowMotor(uint8_t motor_id);

uint32_t Cyber_MakeExtId(uint8_t type, uint16_t data2, uint8_t target_id);
uint16_t Cyber_LimitMap_Int32(int32_t x, int32_t in_min, int32_t in_max);
int32_t Cyber_UintToInt32(uint16_t x, int32_t out_min, int32_t out_max);

uint8_t CAN_Send_Ext(uint32_t ext_id, uint8_t *data, uint8_t len);
uint8_t CAN_Send_Ext_Quick(uint32_t ext_id, uint8_t *data, uint8_t len);
void CAN_PrintStatus(void);

void Cyber_Enable(uint8_t motor_id);
void Cyber_Stop(uint8_t motor_id, uint8_t clear_fault);
void Cyber_SetZero(uint8_t motor_id);
void Cyber_SetMotorID(uint8_t old_id, uint8_t new_id);

void Cyber_ControlMIT(uint8_t motor_id,
                      int32_t pos_mrad,
                      int32_t vel_mrad_s,
                      int32_t kp_x10,
                      int32_t kd_x100,
                      int32_t tq_mNm);

uint8_t Cyber_WriteParam_U8(uint8_t motor_id, uint16_t index, uint8_t value);
uint8_t Cyber_WriteParam_Float(uint8_t motor_id, uint16_t index, float value);
void Cyber_SetRunMode(uint8_t motor_id, uint8_t run_mode);

void Cyber_PositionModeRun(uint8_t motor_id, int32_t pos_mrad, int32_t speed_mrad_s);
void Cyber_SpeedModeRun(uint8_t motor_id, int32_t speed_mrad_s);
void Cyber_CurrentModeRun_mA(uint8_t motor_id, int32_t iq_mA);

void Cyber_RequestDeviceID(uint8_t motor_id);
void Cyber_ScanBus(void);
void Cyber_HandleDeviceID(CanRxMsg* rx);
void Cyber_HandleFeedback(CanRxMsg* rx);
void Cyber_PrintPendingFeedback(void);
void Send_Cyber_Status(uint8_t motor_id);

CyberAngleLimit_t* Cyber_GetLimit(uint8_t motor_id);
uint8_t Cyber_TargetInLimit(uint8_t motor_id, int32_t pos_mrad);
void Cyber_SetAngleLimit(uint8_t motor_id, int32_t min_mrad, int32_t max_mrad);
void Cyber_PrintLimitStatus(void);

int main(void)
{
    NVIC_Config_Init();
    My_GPIO_Init();
    UART1_Init();
    Servo_TIM1_Init();
    Servo_TIM4_Init();
    CAN1_Init_1Mbps_PA11_PA12();

    UART_SendStr("\r\nSTM32 READY: 2 SERVO PUL/DIR + 2 CYBERGEAR\r\n");
    UART_SendStr("USART1: PA9 TX / PA10 RX, 9600\r\n");
    UART_SendStr("CAN1: PA11 RX / PA12 TX, 1Mbps, EXT ID\r\n");
    UART_SendStr("SERVO1: PUL PA8 TIM1_CH1, DIR PB12\r\n");
    UART_SendStr("SERVO2: PUL PB6 TIM4_CH1, DIR PB13\r\n");
    UART_SendStr("SERVO CMD: S1P100 / S2P100 / S1S500 / S2S500 / S1Z / S2Z / SSTOP\r\n");
    UART_SendStr("CYBER CMD KEEP: M1E/M2E/M1POS1000,444/M2SPD1000/SCAN/CAN?/LIMIT?\r\n");

    while(1)
    {
        Cyber_PrintPendingFeedback();

        if(recv_end)
        {
            char cmd_buf[UART_BUF_LEN];

            __disable_irq();
            strncpy(cmd_buf, (char*)uart_buf, UART_BUF_LEN - 1);
            cmd_buf[UART_BUF_LEN - 1] = '\0';
            memset((void*)uart_buf, 0, UART_BUF_LEN);
            uart_cnt = 0;
            recv_end = 0;
            __enable_irq();

            Trim_CRLF(cmd_buf);

            if(cmd_buf[0] != '\0')
            {
                UART_SendStr("RECVD CMD: ");
                UART_SendStr(cmd_buf);
                UART_SendStr("\r\n");
                Parse_Command(cmd_buf);
            }
        }
    }
}

void Delay_Cycles(volatile uint32_t n)
{
    while(n--) {
        __NOP();
    }
}

void Trim_CRLF(char *s)
{
    int n;
    if(s == 0) return;
    n = strlen(s);
    while(n > 0 && (s[n - 1] == '\r' || s[n - 1] == '\n' || s[n - 1] == ' ' || s[n - 1] == '\t')) {
        s[n - 1] = '\0';
        n--;
    }
}

int32_t PosCmd_ToPulse(int pos)
{
    return (int32_t)(((int64_t)pos * SERVO_PULSE_PER_MM) / 10);
}

int32_t I32_Abs(int32_t x)
{
    return (x >= 0) ? x : -x;
}

int32_t Limit_Int32(int32_t x, int32_t min, int32_t max)
{
    if(x < min) return min;
    if(x > max) return max;
    return x;
}

void NVIC_Config_Init(void)
{
    NVIC_InitTypeDef nvic;

    NVIC_PriorityGroupConfig(NVIC_PriorityGroup_2);

    nvic.NVIC_IRQChannel = USART1_IRQn;
    nvic.NVIC_IRQChannelPreemptionPriority = 1;
    nvic.NVIC_IRQChannelSubPriority = 1;
    nvic.NVIC_IRQChannelCmd = ENABLE;
    NVIC_Init(&nvic);

    nvic.NVIC_IRQChannel = USB_LP_CAN1_RX0_IRQn;
    nvic.NVIC_IRQChannelPreemptionPriority = 2;
    nvic.NVIC_IRQChannelSubPriority = 1;
    nvic.NVIC_IRQChannelCmd = ENABLE;
    NVIC_Init(&nvic);

    nvic.NVIC_IRQChannel = TIM1_UP_IRQn;
    nvic.NVIC_IRQChannelPreemptionPriority = 2;
    nvic.NVIC_IRQChannelSubPriority = 2;
    nvic.NVIC_IRQChannelCmd = ENABLE;
    NVIC_Init(&nvic);

    nvic.NVIC_IRQChannel = TIM4_IRQn;
    nvic.NVIC_IRQChannelPreemptionPriority = 2;
    nvic.NVIC_IRQChannelSubPriority = 3;
    nvic.NVIC_IRQChannelCmd = ENABLE;
    NVIC_Init(&nvic);
}

void My_GPIO_Init(void)
{
    GPIO_InitTypeDef g;

    RCC_APB2PeriphClockCmd(
        RCC_APB2Periph_GPIOA |
        RCC_APB2Periph_GPIOB |
        RCC_APB2Periph_AFIO,
        ENABLE
    );

    GPIO_PinRemapConfig(GPIO_Remap_SWJ_JTAGDisable, ENABLE);

    /* DIR: S1 PB12, S2 PB13 */
    g.GPIO_Pin = GPIO_Pin_12 | GPIO_Pin_13;
    g.GPIO_Mode = GPIO_Mode_Out_PP;
    g.GPIO_Speed = GPIO_Speed_50MHz;
    GPIO_Init(GPIOB, &g);
    GPIO_ResetBits(GPIOB, GPIO_Pin_12 | GPIO_Pin_13);

    /* PUL: S1 PA8 TIM1_CH1 */
    g.GPIO_Pin = GPIO_Pin_8;
    g.GPIO_Mode = GPIO_Mode_AF_PP;
    g.GPIO_Speed = GPIO_Speed_50MHz;
    GPIO_Init(GPIOA, &g);

    /* PUL: S2 PB6 TIM4_CH1 */
    g.GPIO_Pin = GPIO_Pin_6;
    g.GPIO_Mode = GPIO_Mode_AF_PP;
    g.GPIO_Speed = GPIO_Speed_50MHz;
    GPIO_Init(GPIOB, &g);

    /* CAN1 PA11 RX / PA12 TX */
    g.GPIO_Pin = GPIO_Pin_11;
    g.GPIO_Mode = GPIO_Mode_IPU;
    GPIO_Init(GPIOA, &g);

    g.GPIO_Pin = GPIO_Pin_12;
    g.GPIO_Mode = GPIO_Mode_AF_PP;
    g.GPIO_Speed = GPIO_Speed_50MHz;
    GPIO_Init(GPIOA, &g);
}

void UART1_Init(void)
{
    GPIO_InitTypeDef g;
    USART_InitTypeDef u;

    RCC_APB2PeriphClockCmd(RCC_APB2Periph_GPIOA | RCC_APB2Periph_USART1, ENABLE);

    /* PA9 TX */
    g.GPIO_Pin = GPIO_Pin_9;
    g.GPIO_Mode = GPIO_Mode_AF_PP;
    g.GPIO_Speed = GPIO_Speed_50MHz;
    GPIO_Init(GPIOA, &g);

    /* PA10 RX */
    g.GPIO_Pin = GPIO_Pin_10;
    g.GPIO_Mode = GPIO_Mode_IN_FLOATING;
    GPIO_Init(GPIOA, &g);

    u.USART_BaudRate = 9600;
    u.USART_WordLength = USART_WordLength_8b;
    u.USART_StopBits = USART_StopBits_1;
    u.USART_Parity = USART_Parity_No;
    u.USART_HardwareFlowControl = USART_HardwareFlowControl_None;
    u.USART_Mode = USART_Mode_Rx | USART_Mode_Tx;
    USART_Init(USART1, &u);

    USART_ITConfig(USART1, USART_IT_RXNE, ENABLE);
    USART_Cmd(USART1, ENABLE);

    if(USART_GetFlagStatus(USART1, USART_FLAG_RXNE) == SET) {
        (void)USART_ReceiveData(USART1);
    }

    memset((void*)uart_buf, 0, UART_BUF_LEN);
    uart_cnt = 0;
    recv_end = 0;
}

void UART_SendByte(uint8_t ch)
{
    while(USART_GetFlagStatus(USART1, USART_FLAG_TXE) == RESET);
    USART_SendData(USART1, ch);
}

void UART_SendStr(const char *str)
{
    while(*str) {
        UART_SendByte((uint8_t)*str);
        str++;
    }
}

void USART1_IRQHandler(void)
{
    if(USART_GetITStatus(USART1, USART_IT_RXNE) != RESET)
    {
        uint8_t ch;
        ch = USART_ReceiveData(USART1);

        if(recv_end == 1) {
            return;
        }

        if(ch == '\r') {
            return;
        }

        if(uart_cnt < UART_BUF_LEN - 1)
        {
            if(ch == '\n') {
                uart_buf[uart_cnt] = '\0';
                recv_end = 1;
            } else {
                uart_buf[uart_cnt] = ch;
                uart_cnt++;
            }
        }
        else
        {
            memset((void*)uart_buf, 0, UART_BUF_LEN);
            uart_cnt = 0;
            recv_end = 0;
            UART_SendStr("UART RX OVERFLOW\r\n");
        }
    }
}

void Servo_TIM1_Init(void)
{
    TIM_TimeBaseInitTypeDef t;
    TIM_OCInitTypeDef oc;

    RCC_APB2PeriphClockCmd(RCC_APB2Periph_TIM1, ENABLE);

    t.TIM_Period = servo1.speed_us - 1;
    t.TIM_Prescaler = 71; /* 72MHz / 72 = 1MHz, 1 tick = 1us */
    t.TIM_CounterMode = TIM_CounterMode_Up;
    t.TIM_ClockDivision = 0;
    t.TIM_RepetitionCounter = 0;
    TIM_TimeBaseInit(TIM1, &t);

    oc.TIM_OCMode = TIM_OCMode_PWM1;
    oc.TIM_OutputState = ENABLE;
    oc.TIM_OutputNState = DISABLE;
    oc.TIM_Pulse = 0;
    oc.TIM_OCPolarity = TIM_OCPolarity_High;
    oc.TIM_OCNPolarity = TIM_OCNPolarity_High;
    oc.TIM_OCIdleState = TIM_OCIdleState_Reset;
    oc.TIM_OCNIdleState = TIM_OCNIdleState_Reset;
    TIM_OC1Init(TIM1, &oc);

    TIM_OC1PreloadConfig(TIM1, TIM_OCPreload_Enable);
    TIM_ARRPreloadConfig(TIM1, ENABLE);
    TIM_ITConfig(TIM1, TIM_IT_Update, ENABLE);
    TIM_CtrlPWMOutputs(TIM1, ENABLE);
    TIM_Cmd(TIM1, ENABLE);
}

void Servo_TIM4_Init(void)
{
    TIM_TimeBaseInitTypeDef t;
    TIM_OCInitTypeDef oc;

    RCC_APB1PeriphClockCmd(RCC_APB1Periph_TIM4, ENABLE);

    t.TIM_Period = servo2.speed_us - 1;
    t.TIM_Prescaler = 71;
    t.TIM_CounterMode = TIM_CounterMode_Up;
    t.TIM_ClockDivision = 0;
    TIM_TimeBaseInit(TIM4, &t);

    oc.TIM_OCMode = TIM_OCMode_PWM1;
    oc.TIM_OutputState = ENABLE;
    oc.TIM_Pulse = 0;
    oc.TIM_OCPolarity = TIM_OCPolarity_High;
    TIM_OC1Init(TIM4, &oc);

    TIM_OC1PreloadConfig(TIM4, TIM_OCPreload_Enable);
    TIM_ARRPreloadConfig(TIM4, ENABLE);
    TIM_ITConfig(TIM4, TIM_IT_Update, ENABLE);
    TIM_Cmd(TIM4, ENABLE);
}

void Servo_SetDir(uint8_t servo_id, uint8_t dir)
{
    if(servo_id == 1) {
        if(dir) GPIO_SetBits(GPIOB, GPIO_Pin_12);
        else GPIO_ResetBits(GPIOB, GPIO_Pin_12);
    } else if(servo_id == 2) {
        if(dir) GPIO_SetBits(GPIOB, GPIO_Pin_13);
        else GPIO_ResetBits(GPIOB, GPIO_Pin_13);
    }
}

void Servo_StartPWM(uint8_t servo_id)
{
    uint16_t spd;

    if(servo_id == 1) {
        spd = servo1.speed_us;
        TIM_SetAutoreload(TIM1, spd - 1);
        TIM_SetCompare1(TIM1, spd / 2);
    } else if(servo_id == 2) {
        spd = servo2.speed_us;
        TIM_SetAutoreload(TIM4, spd - 1);
        TIM_SetCompare1(TIM4, spd / 2);
    }
}

void Servo_StopPWM(uint8_t servo_id)
{
    if(servo_id == 1) {
        TIM_SetCompare1(TIM1, 0);
    } else if(servo_id == 2) {
        TIM_SetCompare1(TIM4, 0);
    }
}

void Servo_Report(uint8_t servo_id, const char *msg)
{
    char b[96];
    if(servo_id == 1) {
        sprintf(b, "S1 %s POS:%ld\r\n", msg, (long)servo1.current_pos);
    } else {
        sprintf(b, "S2 %s POS:%ld\r\n", msg, (long)servo2.current_pos);
    }
    UART_SendStr(b);
}

void Servo_SetSpeed(uint8_t servo_id, uint16_t speed_us)
{
    volatile ServoMotor_t *m;

    if(speed_us < SERVO_MIN_US) speed_us = SERVO_MIN_US;
    if(speed_us > SERVO_MAX_US) speed_us = SERVO_MAX_US;

    if(servo_id == 1) m = &servo1;
    else if(servo_id == 2) m = &servo2;
    else {
        UART_SendStr("SERVO SPEED ERR: ID\r\n");
        return;
    }

    if(m->is_running) {
        UART_SendStr("SERVO SPEED ERR: RUNNING\r\n");
        return;
    }

    m->speed_us = speed_us;
    if(servo_id == 1) TIM_SetAutoreload(TIM1, speed_us - 1);
    else TIM_SetAutoreload(TIM4, speed_us - 1);

    Servo_Report(servo_id, "SPEED_OK");
}

void Servo_RunAbs(uint8_t servo_id, int pos_cmd)
{
    volatile ServoMotor_t *m;
    int32_t tar;
    int32_t dif;

    if(servo_id == 1) m = &servo1;
    else if(servo_id == 2) m = &servo2;
    else {
        UART_SendStr("SERVO RUN ERR: ID\r\n");
        return;
    }

    Servo_StopPWM(servo_id);
    m->is_running = 0;

    tar = PosCmd_ToPulse(pos_cmd);
    dif = tar - m->current_pos;

    if(dif == 0) {
        m->target_pos = tar;
        Servo_Report(servo_id, "POS_OK");
        return;
    }

    m->target_pos = tar;
    m->dir = (dif > 0) ? 0 : 1;
    Servo_SetDir(servo_id, m->dir);

    m->pulse_remain = I32_Abs(dif);
    m->is_running = 1;

    Servo_StartPWM(servo_id);

    Servo_Report(servo_id, "START");
}

void Servo_SetZero(uint8_t servo_id)
{
    if(servo_id == 1) {
        servo1.current_pos = 0;
        servo1.target_pos = 0;
        Servo_Report(1, "ZERO_SET_OK");
    } else if(servo_id == 2) {
        servo2.current_pos = 0;
        servo2.target_pos = 0;
        Servo_Report(2, "ZERO_SET_OK");
    } else {
        UART_SendStr("SERVO ZERO ERR: ID\r\n");
    }
}

void Servo_Stop(uint8_t servo_id)
{
    if(servo_id == 1) {
        Servo_StopPWM(1);
        servo1.is_running = 0;
        servo1.pulse_remain = 0;
        Servo_Report(1, "STOP");
    } else if(servo_id == 2) {
        Servo_StopPWM(2);
        servo2.is_running = 0;
        servo2.pulse_remain = 0;
        Servo_Report(2, "STOP");
    }
}

void Servo_StopAll(void)
{
    Servo_StopPWM(1);
    Servo_StopPWM(2);

    servo1.is_running = 0;
    servo2.is_running = 0;
    servo1.pulse_remain = 0;
    servo2.pulse_remain = 0;

    UART_SendStr("SSTOP OK\r\n");
}

void TIM1_UP_IRQHandler(void)
{
    if(TIM_GetITStatus(TIM1, TIM_IT_Update) != RESET)
    {
        TIM_ClearITPendingBit(TIM1, TIM_IT_Update);

        if(servo1.is_running)
        {
            if(servo1.pulse_remain > 0)
            {
                servo1.pulse_remain--;
                servo1.current_pos += servo1.dir ? -1 : 1;

                if(servo1.pulse_remain <= 0)
                {
                    Servo_StopPWM(1);
                    servo1.is_running = 0;
                    servo1.current_pos = servo1.target_pos;
                    UART_SendStr("S1 POS_OK\r\n");
                }
            }
            else
            {
                Servo_StopPWM(1);
                servo1.is_running = 0;
                UART_SendStr("S1 POS_OK\r\n");
            }
        }
    }
}

void TIM4_IRQHandler(void)
{
    if(TIM_GetITStatus(TIM4, TIM_IT_Update) != RESET)
    {
        TIM_ClearITPendingBit(TIM4, TIM_IT_Update);

        if(servo2.is_running)
        {
            if(servo2.pulse_remain > 0)
            {
                servo2.pulse_remain--;
                servo2.current_pos += servo2.dir ? -1 : 1;

                if(servo2.pulse_remain <= 0)
                {
                    Servo_StopPWM(2);
                    servo2.is_running = 0;
                    servo2.current_pos = servo2.target_pos;
                    UART_SendStr("S2 POS_OK\r\n");
                }
            }
            else
            {
                Servo_StopPWM(2);
                servo2.is_running = 0;
                UART_SendStr("S2 POS_OK\r\n");
            }
        }
    }
}

/* ==================== CAN1 ==================== */
void CAN1_Init_1Mbps_PA11_PA12(void)
{
    CAN_InitTypeDef can;
    CAN_FilterInitTypeDef filter;

    RCC_APB1PeriphClockCmd(RCC_APB1Periph_CAN1, ENABLE);

    CAN_DeInit(CAN1);
    CAN_StructInit(&can);

    can.CAN_TTCM = DISABLE;
    can.CAN_ABOM = ENABLE;
    can.CAN_AWUM = ENABLE;
    can.CAN_NART = DISABLE;
    can.CAN_RFLM = DISABLE;
    can.CAN_TXFP = DISABLE;
    can.CAN_Mode = CAN_Mode_Normal;

    /* APB1 = 36MHz: 36MHz / 4 / (1 + 6 + 2) = 1Mbps */
    can.CAN_SJW = CAN_SJW_1tq;
    can.CAN_BS1 = CAN_BS1_6tq;
    can.CAN_BS2 = CAN_BS2_2tq;
    can.CAN_Prescaler = 4;

    if(CAN_Init(CAN1, &can) != CAN_InitStatus_Success) {
        UART_SendStr("CAN INIT ERR\r\n");
    } else {
        UART_SendStr("CAN INIT OK\r\n");
    }

    filter.CAN_FilterNumber = 0;
    filter.CAN_FilterMode = CAN_FilterMode_IdMask;
    filter.CAN_FilterScale = CAN_FilterScale_32bit;
    filter.CAN_FilterIdHigh = 0x0000;
    filter.CAN_FilterIdLow = 0x0000;
    filter.CAN_FilterMaskIdHigh = 0x0000;
    filter.CAN_FilterMaskIdLow = 0x0000;
    filter.CAN_FilterFIFOAssignment = CAN_Filter_FIFO0;
    filter.CAN_FilterActivation = ENABLE;
    CAN_FilterInit(&filter);

    CAN_ITConfig(CAN1, CAN_IT_FMP0, ENABLE);
}

uint32_t Cyber_MakeExtId(uint8_t type, uint16_t data2, uint8_t target_id)
{
    return (((uint32_t)(type & 0x1F)) << 24) |
           (((uint32_t)data2) << 8) |
           ((uint32_t)target_id);
}

uint16_t Cyber_LimitMap_Int32(int32_t x, int32_t in_min, int32_t in_max)
{
    int64_t num;
    int64_t den;

    if(x < in_min) x = in_min;
    if(x > in_max) x = in_max;

    num = (int64_t)(x - in_min) * 65535;
    den = (int64_t)(in_max - in_min);

    return (uint16_t)(num / den);
}

int32_t Cyber_UintToInt32(uint16_t x, int32_t out_min, int32_t out_max)
{
    int64_t num;
    num = (int64_t)x * (out_max - out_min);
    return (int32_t)(num / 65535 + out_min);
}

uint8_t CAN_Send_Ext(uint32_t ext_id, uint8_t *data, uint8_t len)
{
    CanTxMsg tx;
    uint8_t mailbox;
    uint32_t wait;
    uint8_t status;
    uint8_t i;

    wait = 0;

    tx.StdId = 0;
    tx.ExtId = ext_id;
    tx.IDE = CAN_Id_Extended;
    tx.RTR = CAN_RTR_Data;
    tx.DLC = len;

    for(i = 0; i < 8; i++) {
        tx.Data[i] = (i < len) ? data[i] : 0;
    }

    mailbox = CAN_Transmit(CAN1, &tx);

    if(mailbox == CAN_TxStatus_NoMailBox) {
        UART_SendStr("CAN TX ERR: NO MAILBOX\r\n");
        return 0;
    }

    while(1)
    {
        status = CAN_TransmitStatus(CAN1, mailbox);

        if(status == CAN_TxStatus_Ok) {
            return 1;
        }

        if(status == CAN_TxStatus_Failed) {
            CAN_CancelTransmit(CAN1, mailbox);
            UART_SendStr("CAN TX FAILED: NO ACK / BUS ERR\r\n");
            return 0;
        }

        wait++;
        if(wait > 300000)
        {
            CAN_CancelTransmit(CAN1, mailbox);
            UART_SendStr("CAN TX TIMEOUT: NO ACK / BUS OFF / WIRING ERR\r\n");
            return 0;
        }
    }
}

uint8_t CAN_Send_Ext_Quick(uint32_t ext_id, uint8_t *data, uint8_t len)
{
    CanTxMsg tx;
    uint8_t mailbox;
    uint32_t wait;
    uint8_t status;
    uint8_t i;

    wait = 0;

    tx.StdId = 0;
    tx.ExtId = ext_id;
    tx.IDE = CAN_Id_Extended;
    tx.RTR = CAN_RTR_Data;
    tx.DLC = len;

    for(i = 0; i < 8; i++) {
        tx.Data[i] = (i < len) ? data[i] : 0;
    }

    mailbox = CAN_Transmit(CAN1, &tx);

    if(mailbox == CAN_TxStatus_NoMailBox) {
        return 0;
    }

    while(1)
    {
        status = CAN_TransmitStatus(CAN1, mailbox);

        if(status == CAN_TxStatus_Ok) {
            return 1;
        }

        if(status == CAN_TxStatus_Failed) {
            CAN_CancelTransmit(CAN1, mailbox);
            return 0;
        }

        wait++;
        if(wait > 8000)
        {
            CAN_CancelTransmit(CAN1, mailbox);
            return 0;
        }
    }
}

void CAN_PrintStatus(void)
{
    char buf[160];

    sprintf(buf,
            "CAN MSR:0x%08lX TSR:0x%08lX RF0R:0x%08lX ESR:0x%08lX\r\n",
            (unsigned long)CAN1->MSR,
            (unsigned long)CAN1->TSR,
            (unsigned long)CAN1->RF0R,
            (unsigned long)CAN1->ESR);
    UART_SendStr(buf);

    sprintf(buf,
            "CAN TEC:%lu REC:%lu LEC:%lu BOFF:%lu EPVF:%lu EWGF:%lu\r\n",
            (unsigned long)((CAN1->ESR >> 16) & 0xFF),
            (unsigned long)((CAN1->ESR >> 24) & 0xFF),
            (unsigned long)((CAN1->ESR >> 4) & 0x07),
            (unsigned long)((CAN1->ESR >> 2) & 0x01),
            (unsigned long)((CAN1->ESR >> 1) & 0x01),
            (unsigned long)(CAN1->ESR & 0x01));
    UART_SendStr(buf);
}

uint8_t Comm_AllowMotor(uint8_t motor_id)
{
    if(comm_select == COMM_ALL) return 1;
    if(comm_select == COMM_CAN1 && motor_id == 1) return 1;
    if(comm_select == COMM_CAN2 && motor_id == 2) return 1;
    return 0;
}

void Send_Comm_Select_Report(void)
{
    if(comm_select == COMM_MAIN) UART_SendStr("COMM SELECT: MAIN\r\n");
    else if(comm_select == COMM_CAN1) UART_SendStr("COMM SELECT: CAN1 / M1\r\n");
    else if(comm_select == COMM_CAN2) UART_SendStr("COMM SELECT: CAN2 / M2\r\n");
    else UART_SendStr("COMM SELECT: ALL\r\n");
}

void Cyber_Enable(uint8_t motor_id)
{
    uint8_t data[8];
    uint32_t id;
    char buf[64];
    uint8_t i;

    if(!Comm_AllowMotor(motor_id)) {
        UART_SendStr("CYBER SKIP: COMM NOT SELECTED\r\n");
        return;
    }

    for(i = 0; i < 8; i++) data[i] = 0;

    id = Cyber_MakeExtId(3, ((uint16_t)CYBER_MASTER_ID << 8), motor_id);

    if(CAN_Send_Ext(id, data, 8)) {
        sprintf(buf, "CYBER ID:%d ENABLE TX_OK\r\n", motor_id);
    } else {
        sprintf(buf, "CYBER ID:%d ENABLE TX_FAIL\r\n", motor_id);
    }
    UART_SendStr(buf);
}

void Cyber_Stop(uint8_t motor_id, uint8_t clear_fault)
{
    uint8_t data[8];
    uint32_t id;
    char buf[64];
    uint8_t i;

    if(!Comm_AllowMotor(motor_id) && clear_fault == 0) {
        UART_SendStr("CYBER STOP SKIP: COMM NOT SELECTED\r\n");
        return;
    }

    for(i = 0; i < 8; i++) data[i] = 0;
    data[0] = clear_fault ? 1 : 0;

    id = Cyber_MakeExtId(4, ((uint16_t)CYBER_MASTER_ID << 8), motor_id);

    if(CAN_Send_Ext(id, data, 8)) {
        sprintf(buf, "CYBER ID:%d STOP TX_OK\r\n", motor_id);
    } else {
        sprintf(buf, "CYBER ID:%d STOP TX_FAIL\r\n", motor_id);
    }
    UART_SendStr(buf);
}

void Cyber_SetZero(uint8_t motor_id)
{
    uint8_t data[8];
    uint32_t id;
    char buf[64];
    uint8_t i;

    if(!Comm_AllowMotor(motor_id)) {
        UART_SendStr("CYBER SKIP: COMM NOT SELECTED\r\n");
        return;
    }

    for(i = 0; i < 8; i++) data[i] = 0;
    data[0] = 1;

    id = Cyber_MakeExtId(6, ((uint16_t)CYBER_MASTER_ID << 8), motor_id);

    if(CAN_Send_Ext(id, data, 8)) {
        sprintf(buf, "CYBER ID:%d SET_ZERO TX_OK\r\n", motor_id);
    } else {
        sprintf(buf, "CYBER ID:%d SET_ZERO TX_FAIL\r\n", motor_id);
    }
    UART_SendStr(buf);
}

void Cyber_SetMotorID(uint8_t old_id, uint8_t new_id)
{
    uint8_t data[8];
    uint16_t data2;
    uint32_t id;
    char buf[96];
    uint8_t i;

    for(i = 0; i < 8; i++) data[i] = 0;

    if(old_id == 0 || old_id > 127 || new_id == 0 || new_id > 127)
    {
        UART_SendStr("SETID ERR: ID RANGE 1-127\r\n");
        return;
    }

    data[0] = 1;

    data2 = ((uint16_t)new_id << 8) | (uint16_t)CYBER_MASTER_ID;
    id = Cyber_MakeExtId(7, data2, old_id);

    if(CAN_Send_Ext(id, data, 8))
    {
        sprintf(buf, "SETID TX_OK OLD:%d NEW:%d\r\n", old_id, new_id);
        UART_SendStr(buf);
        UART_SendStr("PLEASE POWER CYCLE MOTOR, THEN SCAN AGAIN\r\n");
    }
    else
    {
        sprintf(buf, "SETID TX_FAIL OLD:%d NEW:%d\r\n", old_id, new_id);
        UART_SendStr(buf);
    }
}

void Cyber_ControlMIT(uint8_t motor_id,
                      int32_t pos_mrad,
                      int32_t vel_mrad_s,
                      int32_t kp_x10,
                      int32_t kd_x100,
                      int32_t tq_mNm)
{
    uint16_t pos_u;
    uint16_t vel_u;
    uint16_t kp_u;
    uint16_t kd_u;
    uint16_t tq_u;
    uint8_t data[8];
    uint32_t id;
    char buf[128];

    if(!Comm_AllowMotor(motor_id)) {
        UART_SendStr("CYBER SKIP: COMM NOT SELECTED\r\n");
        return;
    }

    if(!Cyber_TargetInLimit(motor_id, pos_mrad)) {
        UART_SendStr("CYBER MIT ERR: POS OUT OF LIMIT\r\n");
        return;
    }

    pos_u = Cyber_LimitMap_Int32(pos_mrad, POS_MIN_MRAD, POS_MAX_MRAD);
    vel_u = Cyber_LimitMap_Int32(vel_mrad_s, VEL_MIN_MRAD_S, VEL_MAX_MRAD_S);
    kp_u  = Cyber_LimitMap_Int32(kp_x10, KP_MIN_X10, KP_MAX_X10);
    kd_u  = Cyber_LimitMap_Int32(kd_x100, KD_MIN_X100, KD_MAX_X100);
    tq_u  = Cyber_LimitMap_Int32(tq_mNm, TQ_MIN_MNM, TQ_MAX_MNM);

    data[0] = (uint8_t)(pos_u >> 8);
    data[1] = (uint8_t)(pos_u & 0xFF);
    data[2] = (uint8_t)(vel_u >> 8);
    data[3] = (uint8_t)(vel_u & 0xFF);
    data[4] = (uint8_t)(kp_u >> 8);
    data[5] = (uint8_t)(kp_u & 0xFF);
    data[6] = (uint8_t)(kd_u >> 8);
    data[7] = (uint8_t)(kd_u & 0xFF);

    id = Cyber_MakeExtId(1, tq_u, motor_id);

    if(CAN_Send_Ext(id, data, 8))
    {
        sprintf(buf, "M%d MIT TX_OK P:%ld V:%ld KP:%ld KD:%ld TQ:%ld\r\n",
                motor_id, (long)pos_mrad, (long)vel_mrad_s,
                (long)kp_x10, (long)kd_x100, (long)tq_mNm);
        UART_SendStr(buf);
    }
    else
    {
        UART_SendStr("MIT TX_FAIL\r\n");
    }
}

uint8_t Cyber_WriteParam_U8(uint8_t motor_id, uint16_t index, uint8_t value)
{
    uint8_t data[8];
    uint32_t id;
    uint8_t i;

    for(i = 0; i < 8; i++) data[i] = 0;

    data[0] = (uint8_t)(index & 0xFF);
    data[1] = (uint8_t)(index >> 8);
    data[4] = value;

    id = Cyber_MakeExtId(18, ((uint16_t)CYBER_MASTER_ID << 8), motor_id);
    return CAN_Send_Ext(id, data, 8);
}

uint8_t Cyber_WriteParam_Float(uint8_t motor_id, uint16_t index, float value)
{
    uint8_t data[8];
    uint32_t id;
    union {
        float f;
        uint8_t b[4];
    } u;
    uint8_t i;

    for(i = 0; i < 8; i++) data[i] = 0;

    u.f = value;

    data[0] = (uint8_t)(index & 0xFF);
    data[1] = (uint8_t)(index >> 8);
    data[4] = u.b[0];
    data[5] = u.b[1];
    data[6] = u.b[2];
    data[7] = u.b[3];

    id = Cyber_MakeExtId(18, ((uint16_t)CYBER_MASTER_ID << 8), motor_id);
    return CAN_Send_Ext(id, data, 8);
}

void Cyber_SetRunMode(uint8_t motor_id, uint8_t run_mode)
{
    char buf[64];

    if(run_mode > 3)
    {
        UART_SendStr("MODE ERR: RANGE 0-3\r\n");
        return;
    }

    if(!Comm_AllowMotor(motor_id)) {
        UART_SendStr("CYBER SKIP: COMM NOT SELECTED\r\n");
        return;
    }

    Cyber_Stop(motor_id, 0);
    Delay_Cycles(50000);

    if(Cyber_WriteParam_U8(motor_id, CYBER_INDEX_RUN_MODE, run_mode))
    {
        sprintf(buf, "M%d MODE%d TX_OK\r\n", motor_id, run_mode);
        UART_SendStr(buf);
    }
    else
    {
        UART_SendStr("MODE TX_FAIL\r\n");
    }
}

void Cyber_PositionModeRun(uint8_t motor_id, int32_t pos_mrad, int32_t speed_mrad_s)
{
    float pos_rad;
    float spd_rad_s;
    char buf[128];
    uint8_t ok;

    if(!Comm_AllowMotor(motor_id)) {
        UART_SendStr("CYBER SKIP: COMM NOT SELECTED\r\n");
        return;
    }

    if(!Cyber_TargetInLimit(motor_id, pos_mrad)) {
        UART_SendStr("CYBER POS ERR: TARGET OUT OF LIMIT\r\n");
        return;
    }

    speed_mrad_s = Limit_Int32(speed_mrad_s, 1, VEL_MAX_MRAD_S);
    pos_rad = (float)pos_mrad / 1000.0f;
    spd_rad_s = (float)speed_mrad_s / 1000.0f;

    /*
     * CyberGear 原生位置模式更稳的顺序：
     * 1. 停止
     * 2. 切 run_mode=1
     * 3. 写限流、限速
     * 4. 使能
     * 5. 使能后再写 loc_ref 目标位置
     *
     * 部分电机/固件在未使能时写 loc_ref，不会真正开始执行，
     * 所以这里把 loc_ref 放到 Enable 后，并且补发一次。
     */
    Cyber_Stop(motor_id, 0);
    Delay_Cycles(120000);

    ok = Cyber_WriteParam_U8(motor_id, CYBER_INDEX_RUN_MODE, CYBER_RUN_MODE_POS);
    sprintf(buf, "M%d WRITE RUN_MODE POS:%s\r\n", motor_id, ok ? "OK" : "FAIL");
    UART_SendStr(buf);
    if(!ok) return;
    Delay_Cycles(80000);

    ok = Cyber_WriteParam_Float(motor_id, CYBER_INDEX_LIMIT_CUR, DEFAULT_LIMIT_CUR_A);
    sprintf(buf, "M%d WRITE LIMIT_CUR:%s\r\n", motor_id, ok ? "OK" : "FAIL");
    UART_SendStr(buf);
    if(!ok) return;
    Delay_Cycles(80000);

    ok = Cyber_WriteParam_Float(motor_id, CYBER_INDEX_LIMIT_SPD, spd_rad_s);
    sprintf(buf, "M%d WRITE LIMIT_SPD:%s %.3frad/s\r\n", motor_id, ok ? "OK" : "FAIL", spd_rad_s);
    UART_SendStr(buf);
    if(!ok) return;
    Delay_Cycles(80000);

    Cyber_Enable(motor_id);
    Delay_Cycles(150000);

    ok = Cyber_WriteParam_Float(motor_id, CYBER_INDEX_LOC_REF, pos_rad);
    sprintf(buf, "M%d WRITE LOC_REF:%s %.3frad\r\n", motor_id, ok ? "OK" : "FAIL", pos_rad);
    UART_SendStr(buf);
    if(!ok) return;
    Delay_Cycles(80000);

    ok = Cyber_WriteParam_Float(motor_id, CYBER_INDEX_LOC_REF, pos_rad);
    sprintf(buf, "M%d WRITE LOC_REF AGAIN:%s %.3frad\r\n", motor_id, ok ? "OK" : "FAIL", pos_rad);
    UART_SendStr(buf);

    sprintf(buf, "M%d POS RUN P:%ldmrad SPD:%ldmrad/s\r\n",
            motor_id, (long)pos_mrad, (long)speed_mrad_s);
    UART_SendStr(buf);
}



void Cyber_SpeedModeRun(uint8_t motor_id, int32_t speed_mrad_s)
{
    float spd_rad_s;
    char buf[128];
    uint8_t ok;

    if(!Comm_AllowMotor(motor_id)) {
        UART_SendStr("CYBER SKIP: COMM NOT SELECTED\r\n");
        return;
    }

    speed_mrad_s = Limit_Int32(speed_mrad_s, VEL_MIN_MRAD_S, VEL_MAX_MRAD_S);
    spd_rad_s = (float)speed_mrad_s / 1000.0f;

    Cyber_Stop(motor_id, 0);
    Delay_Cycles(120000);

    ok = Cyber_WriteParam_U8(motor_id, CYBER_INDEX_RUN_MODE, CYBER_RUN_MODE_SPEED);
    sprintf(buf, "M%d WRITE RUN_MODE SPD:%s\r\n", motor_id, ok ? "OK" : "FAIL");
    UART_SendStr(buf);
    if(!ok) return;
    Delay_Cycles(80000);

    ok = Cyber_WriteParam_Float(motor_id, CYBER_INDEX_LIMIT_CUR, DEFAULT_LIMIT_CUR_A);
    sprintf(buf, "M%d WRITE LIMIT_CUR:%s\r\n", motor_id, ok ? "OK" : "FAIL");
    UART_SendStr(buf);
    if(!ok) return;
    Delay_Cycles(80000);

    Cyber_Enable(motor_id);
    Delay_Cycles(150000);

    ok = Cyber_WriteParam_Float(motor_id, CYBER_INDEX_SPD_REF, spd_rad_s);
    sprintf(buf, "M%d WRITE SPD_REF:%s %.3frad/s\r\n", motor_id, ok ? "OK" : "FAIL", spd_rad_s);
    UART_SendStr(buf);
    if(!ok) return;

    sprintf(buf, "M%d SPD RUN:%ldmrad/s\r\n", motor_id, (long)speed_mrad_s);
    UART_SendStr(buf);
}



void Cyber_CurrentModeRun_mA(uint8_t motor_id, int32_t iq_mA)
{
    float iq_a;
    char buf[96];

    if(!Comm_AllowMotor(motor_id)) {
        UART_SendStr("CYBER SKIP: COMM NOT SELECTED\r\n");
        return;
    }

    iq_mA = Limit_Int32(iq_mA, -10000, 10000);
    iq_a = (float)iq_mA / 1000.0f;

    Cyber_Stop(motor_id, 0);
    Delay_Cycles(50000);
    Cyber_WriteParam_U8(motor_id, CYBER_INDEX_RUN_MODE, CYBER_RUN_MODE_CURRENT);
    Delay_Cycles(30000);
    Cyber_WriteParam_Float(motor_id, CYBER_INDEX_LIMIT_CUR, DEFAULT_LIMIT_CUR_A);
    Delay_Cycles(30000);
    Cyber_WriteParam_Float(motor_id, CYBER_INDEX_IQ_REF, iq_a);
    Delay_Cycles(30000);
    Cyber_Enable(motor_id);

    sprintf(buf, "M%d IQ RUN:%ldmA\r\n", motor_id, (long)iq_mA);
    UART_SendStr(buf);
}

void Cyber_RequestDeviceID(uint8_t motor_id)
{
    uint8_t data[8];
    uint32_t id;
    uint8_t i;

    for(i = 0; i < 8; i++) data[i] = 0;
    id = Cyber_MakeExtId(0, ((uint16_t)CYBER_MASTER_ID << 8), motor_id);
    CAN_Send_Ext_Quick(id, data, 8);
}

void Cyber_ScanBus(void)
{
    uint8_t id;
    char buf[64];

    memset((void*)cyber_scan_found, 0, sizeof(cyber_scan_found));

    UART_SendStr("SCAN START 1-127\r\n");

    for(id = 1; id <= CYBER_SCAN_MAX_ID; id++)
    {
        Cyber_RequestDeviceID(id);
        Delay_Cycles(8000);
    }

    UART_SendStr("SCAN TX DONE. WAIT FEEDBACK.\r\n");

    for(id = 1; id <= CYBER_SCAN_MAX_ID; id++)
    {
        if(cyber_scan_found[id])
        {
            sprintf(buf, "SCAN FOUND ID:%d\r\n", id);
            UART_SendStr(buf);
        }
    }
}

void Cyber_HandleDeviceID(CanRxMsg* rx)
{
    uint8_t id;
    uint8_t type;
    uint32_t ext;

    ext = rx->ExtId;
    type = (uint8_t)((ext >> 24) & 0x1F);
    id = (uint8_t)(ext & 0xFF);

    if(id < 128) {
        cyber_scan_found[id] = 1;
    }

    if(type == 0) {
        char buf[64];
        sprintf(buf, "DEVICE ID FEEDBACK FROM:%d\r\n", id);
        UART_SendStr(buf);
    }
}

void Cyber_HandleFeedback(CanRxMsg* rx)
{
    uint8_t id;
    uint32_t ext;
    CyberMotor_t *m;
    uint16_t pos_u;
    uint16_t vel_u;
    uint16_t tq_u;
    uint16_t temp_u;

    ext = rx->ExtId;
    id = (uint8_t)(ext & 0xFF);

    if(id == CYBER_M1_ID) m = &cyber1;
    else if(id == CYBER_M2_ID) m = &cyber2;
    else return;

    if(rx->DLC < 8) return;

    pos_u  = ((uint16_t)rx->Data[0] << 8) | rx->Data[1];
    vel_u  = ((uint16_t)rx->Data[2] << 8) | rx->Data[3];
    tq_u   = ((uint16_t)rx->Data[4] << 8) | rx->Data[5];
    temp_u = ((uint16_t)rx->Data[6] << 8) | rx->Data[7];

    m->pos_mrad = Cyber_UintToInt32(pos_u, POS_MIN_MRAD, POS_MAX_MRAD);
    m->vel_mrad_s = Cyber_UintToInt32(vel_u, VEL_MIN_MRAD_S, VEL_MAX_MRAD_S);
    m->torque_mNm = Cyber_UintToInt32(tq_u, TQ_MIN_MNM, TQ_MAX_MNM);
    m->temp_x10 = (int16_t)temp_u;
    m->feedback_pending = 1;
}

void Cyber_PrintPendingFeedback(void)
{
    char buf[128];

    if(cyber1.feedback_pending)
    {
        cyber1.feedback_pending = 0;
        sprintf(buf, "M1 FB P:%ldmrad V:%ldmrad/s TQ:%ldmNm TEMP:%d\r\n",
                (long)cyber1.pos_mrad, (long)cyber1.vel_mrad_s,
                (long)cyber1.torque_mNm, cyber1.temp_x10);
        UART_SendStr(buf);
    }

    if(cyber2.feedback_pending)
    {
        cyber2.feedback_pending = 0;
        sprintf(buf, "M2 FB P:%ldmrad V:%ldmrad/s TQ:%ldmNm TEMP:%d\r\n",
                (long)cyber2.pos_mrad, (long)cyber2.vel_mrad_s,
                (long)cyber2.torque_mNm, cyber2.temp_x10);
        UART_SendStr(buf);
    }
}

void Send_Cyber_Status(uint8_t motor_id)
{
    CyberMotor_t *m;
    char buf[128];

    if(motor_id == 1) m = &cyber1;
    else if(motor_id == 2) m = &cyber2;
    else {
        UART_SendStr("STATUS ERR: MOTOR ID\r\n");
        return;
    }

    sprintf(buf, "M%d STATUS P:%ld V:%ld TQ:%ld TEMP:%d\r\n",
            motor_id, (long)m->pos_mrad, (long)m->vel_mrad_s,
            (long)m->torque_mNm, m->temp_x10);
    UART_SendStr(buf);
}

CyberAngleLimit_t* Cyber_GetLimit(uint8_t motor_id)
{
    if(motor_id == 1) return &limit1;
    if(motor_id == 2) return &limit2;
    return 0;
}

uint8_t Cyber_TargetInLimit(uint8_t motor_id, int32_t pos_mrad)
{
    CyberAngleLimit_t *l;

    l = Cyber_GetLimit(motor_id);
    if(l == 0) return 0;
    if(!l->enabled) return 1;

    if(pos_mrad < l->min_mrad || pos_mrad > l->max_mrad) return 0;
    return 1;
}

void Cyber_SetAngleLimit(uint8_t motor_id, int32_t min_mrad, int32_t max_mrad)
{
    CyberAngleLimit_t *l;
    char buf[96];

    l = Cyber_GetLimit(motor_id);
    if(l == 0) {
        UART_SendStr("LIMIT ERR: MOTOR ID\r\n");
        return;
    }

    if(min_mrad >= max_mrad) {
        UART_SendStr("LIMIT ERR: MIN >= MAX\r\n");
        return;
    }

    l->enabled = 1;
    l->min_mrad = min_mrad;
    l->max_mrad = max_mrad;

    sprintf(buf, "M%d LIMIT SET [%ld,%ld] mrad\r\n",
            motor_id, (long)min_mrad, (long)max_mrad);
    UART_SendStr(buf);
}

void Cyber_PrintLimitStatus(void)
{
    char buf[128];

    sprintf(buf, "M1 LIMIT EN:%d MIN:%ld MAX:%ld\r\n",
            limit1.enabled, (long)limit1.min_mrad, (long)limit1.max_mrad);
    UART_SendStr(buf);

    sprintf(buf, "M2 LIMIT EN:%d MIN:%ld MAX:%ld\r\n",
            limit2.enabled, (long)limit2.min_mrad, (long)limit2.max_mrad);
    UART_SendStr(buf);
}

void USB_LP_CAN1_RX0_IRQHandler(void)
{
    CanRxMsg rx;
    uint8_t type;

    if(CAN_GetITStatus(CAN1, CAN_IT_FMP0) != RESET)
    {
        CAN_Receive(CAN1, CAN_FIFO0, &rx);

        if(rx.IDE == CAN_Id_Extended)
        {
            type = (uint8_t)((rx.ExtId >> 24) & 0x1F);

            if(type == 2) {
                Cyber_HandleFeedback(&rx);
            } else {
                Cyber_HandleDeviceID(&rx);
            }
        }

        CAN_ClearITPendingBit(CAN1, CAN_IT_FMP0);
    }
}

/* ==================== 指令解析 ==================== */
void Parse_Command(char* buf)
{
    if(strcmp(buf, "STOP") == 0)
    {
        Servo_StopAll();

        comm_select = COMM_ALL;
        Cyber_Stop(CYBER_M1_ID, 0);
        Cyber_Stop(CYBER_M2_ID, 0);
        return;
    }

    if(strcmp(buf, "CLEAR") == 0)
    {
        comm_select = COMM_ALL;
        Cyber_Stop(CYBER_M1_ID, 1);
        Cyber_Stop(CYBER_M2_ID, 1);
        UART_SendStr("CLEAR SENT\r\n");
        return;
    }

    if(strcmp(buf, "SSTOP") == 0)
    {
        Servo_StopAll();
        return;
    }

    if(strcmp(buf, "SCAN") == 0)
    {
        Cyber_ScanBus();
        return;
    }

    if(strcmp(buf, "CAN?") == 0)
    {
        CAN_PrintStatus();
        return;
    }

    if(strcmp(buf, "LIMIT?") == 0)
    {
        Cyber_PrintLimitStatus();
        return;
    }

    if(strcmp(buf, "COMM?") == 0)
    {
        Send_Comm_Select_Report();
        return;
    }

    if(strcmp(buf, "COMM:MAIN") == 0)
    {
        comm_select = COMM_MAIN;
        Send_Comm_Select_Report();
        return;
    }

    if(strcmp(buf, "COMM:CAN1") == 0)
    {
        comm_select = COMM_CAN1;
        Send_Comm_Select_Report();
        return;
    }

    if(strcmp(buf, "COMM:CAN2") == 0)
    {
        comm_select = COMM_CAN2;
        Send_Comm_Select_Report();
        return;
    }

    if(strcmp(buf, "COMM:ALL") == 0)
    {
        comm_select = COMM_ALL;
        Send_Comm_Select_Report();
        return;
    }

    if(strncmp(buf, "SETID", 5) == 0)
    {
        char *comma;
        uint8_t old_id;
        uint8_t new_id;

        comma = strchr(buf + 5, ',');
        if(comma == 0) {
            UART_SendStr("SETID ERR: SETIDold,new\r\n");
            return;
        }

        old_id = (uint8_t)atoi(buf + 5);
        new_id = (uint8_t)atoi(comma + 1);
        Cyber_SetMotorID(old_id, new_id);
        return;
    }

    /* S1/S2 PUL-DIR 伺服命令 */
    if((buf[0] == 'S' || buf[0] == 's') && (buf[1] == '1' || buf[1] == '2'))
    {
        uint8_t sid;
        char *cmd;

        sid = (uint8_t)(buf[1] - '0');
        cmd = buf + 2;

        if(strcmp(cmd, "STOP") == 0)
        {
            Servo_Stop(sid);
            return;
        }

        if(strcmp(cmd, "Z") == 0)
        {
            Servo_SetZero(sid);
            return;
        }

        if(cmd[0] == 'S')
        {
            Servo_SetSpeed(sid, (uint16_t)atoi(cmd + 1));
            return;
        }

        if(cmd[0] == 'P')
        {
            Servo_RunAbs(sid, atoi(cmd + 1));
            return;
        }

        UART_SendStr("SERVO CMD ERR\r\n");
        return;
    }

    /* CyberGear M1/M2 命令 */
    if((buf[0] == 'M' || buf[0] == 'm') && (buf[1] == '1' || buf[1] == '2'))
    {
        uint8_t mid;
        char *cmd;

        mid = (uint8_t)(buf[1] - '0');
        cmd = buf + 2;

        if(strcmp(cmd, "E") == 0)
        {
            Cyber_Enable(mid);
            return;
        }

        if(strcmp(cmd, "STOP") == 0)
        {
            Cyber_Stop(mid, 0);
            return;
        }

        if(strcmp(cmd, "Z") == 0)
        {
            Cyber_SetZero(mid);
            return;
        }

        if(strcmp(cmd, "?") == 0)
        {
            Send_Cyber_Status(mid);
            return;
        }

        if(strncmp(cmd, "MODE", 4) == 0)
        {
            Cyber_SetRunMode(mid, (uint8_t)atoi(cmd + 4));
            return;
        }

        if(strncmp(cmd, "POS", 3) == 0)
        {
            char *comma;
            int32_t pos;
            int32_t spd;

            comma = strchr(cmd + 3, ',');
            pos = atoi(cmd + 3);
            spd = (comma != 0) ? atoi(comma + 1) : 444;
            Cyber_PositionModeRun(mid, pos, spd);
            return;
        }

        if(strncmp(cmd, "SPD", 3) == 0)
        {
            Cyber_SpeedModeRun(mid, atoi(cmd + 3));
            return;
        }

        if(strncmp(cmd, "IQ", 2) == 0)
        {
            Cyber_CurrentModeRun_mA(mid, atoi(cmd + 2));
            return;
        }

        if(cmd[0] == 'L')
        {
            char *comma;
            int32_t mn;
            int32_t mx;

            comma = strchr(cmd + 1, ',');
            if(comma == 0) {
                UART_SendStr("LIMIT CMD ERR: MxLmin,max\r\n");
                return;
            }

            mn = atoi(cmd + 1);
            mx = atoi(comma + 1);
            Cyber_SetAngleLimit(mid, mn, mx);
            return;
        }

        if(cmd[0] == 'C')
        {
            char temp[96];
            char *p;
            int32_t values[5];
            int i;

            strncpy(temp, cmd + 1, sizeof(temp) - 1);
            temp[sizeof(temp) - 1] = '\0';

            p = strtok(temp, ",");
            for(i = 0; i < 5; i++)
            {
                if(p == 0) {
                    UART_SendStr("MIT CMD ERR: MxCpos,vel,kp,kd,tq\r\n");
                    return;
                }

                values[i] = atoi(p);
                p = strtok(0, ",");
            }

            Cyber_ControlMIT(mid, values[0], values[1], values[2], values[3], values[4]);
            return;
        }

        UART_SendStr("CYBER CMD ERR\r\n");
        return;
    }

    UART_SendStr("UNKNOWN CMD\r\n");
}
