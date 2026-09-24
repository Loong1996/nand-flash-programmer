// Behavioural Gowin rPLL for simulation (RTL and gate level): CLKOUT =
// CLKIN * (FBDIV_SEL + 1) / (IDIV_SEL + 1), measured from the input period
// and re-aligned to every rising CLKIN edge (phase locked); LOCK rises after
// 20 input cycles. With DYN_DA_EN = "true", CLKOUTP lags CLKOUT by PSDA / 16
// of its period (DUTYDA is assumed to keep the 50 % duty cycle). Only the
// static-divider mode is modelled.
`timescale 1ns/1ps

module rPLL #(
    parameter FCLKIN        = "100.0",
    parameter DYN_IDIV_SEL  = "false",
    parameter IDIV_SEL      = 0,
    parameter DYN_FBDIV_SEL = "false",
    parameter FBDIV_SEL     = 0,
    parameter DYN_ODIV_SEL  = "false",
    parameter ODIV_SEL      = 8,
    parameter PSDA_SEL      = "0000",
    parameter DYN_DA_EN     = "false",
    parameter DUTYDA_SEL    = "1000",
    parameter CLKOUT_FT_DIR = 1'b1,
    parameter CLKOUTP_FT_DIR = 1'b1,
    parameter CLKOUT_DLY_STEP = 0,
    parameter CLKOUTP_DLY_STEP = 0,
    parameter CLKFB_SEL     = "internal",
    parameter CLKOUT_BYPASS = "false",
    parameter CLKOUTP_BYPASS = "false",
    parameter CLKOUTD_BYPASS = "false",
    parameter DYN_SDIV_SEL  = 2,
    parameter CLKOUTD_SRC   = "CLKOUT",
    parameter CLKOUTD3_SRC  = "CLKOUT",
    parameter DEVICE        = "GW1N-1"
) (
    output reg  CLKOUT,
    output wire CLKOUTP,
    output wire CLKOUTD,
    output wire CLKOUTD3,
    output reg  LOCK,
    input  wire CLKIN,
    input  wire CLKFB,
    input  wire RESET,
    input  wire RESET_P,
    input  wire [5:0] FBDSEL,
    input  wire [5:0] IDSEL,
    input  wire [5:0] ODSEL,
    input  wire [3:0] PSDA,
    input  wire [3:0] DUTYDA,
    input  wire [3:0] FDLY
);
    real    t_last, period, half;
    integer n, k;
    reg     clkp;

    assign CLKOUTP  = (DYN_DA_EN == "true") ? clkp : CLKOUT;
    assign CLKOUTD  = 1'b0;
    assign CLKOUTD3 = 1'b0;

    initial begin
        CLKOUT = 1'b0; clkp = 1'b0; LOCK = 1'b0; n = 0; period = 0.0; t_last = 0.0;
    end

    always @(posedge CLKIN) begin
        if (n > 0)
            period = $realtime - t_last;
        t_last = $realtime;
        n = n + 1;
        if (n == 20)
            LOCK = 1'b1;
    end

    // one output burst per input cycle, started on the input edge
    always @(posedge CLKIN) if (period != 0.0) begin
        half = period * (IDIV_SEL + 1) / (FBDIV_SEL + 1) / 2.0;
        CLKOUT = 1'b1;
        for (k = 1; k < 2 * (FBDIV_SEL + 1) / (IDIV_SEL + 1); k = k + 1)
            #(half) CLKOUT = ~CLKOUT;
    end

    real lag;
    always @(CLKOUT) begin
        lag = (^PSDA === 1'bx) ? 0.0 : 2.0 * half * PSDA / 16.0;
        clkp <= #(lag) CLKOUT;
    end
endmodule
