// Behavioural 8-bit asynchronous SLC NAND for simulation, with timing checks.
// Supports: FFh reset, 90h ID (00h / 20h "ONFI"), ECh parameter page,
// 00h-30h page read, 05h-E0h random read, 70h status (and 00h to resume
// data output), 80h/85h-10h program, 60h-D0h block erase.
`timescale 1ns/1ps

module nand_model #(
    parameter PAGE      = 2048,
    parameter SPARE     = 64,
    parameter PPB       = 8,
    parameter BLOCKS    = 8,
    parameter ROWC      = 3,
    parameter COLC      = 2,
    parameter [39:0] ID = 40'h04_95_80_F1_2C,   // byte 0 in bits 7:0
    parameter BAD_BLOCK = 3,
    parameter real T_R    = 3000.0,
    parameter real T_PROG = 6000.0,
    parameter real T_BERS = 9000.0,
    parameter real T_RST  = 1000.0,
    parameter real T_REA  = 20.0
) (
    input  wire       ce_n,
    input  wire       cle,
    input  wire       ale,
    input  wire       we_n,
    input  wire       re_n,
    input  wire       wp_n,
    inout  wire [7:0] io,
    output wire       rb_n
);
    localparam PB   = PAGE + SPARE;
    localparam SIZE = PPB * BLOCKS * PB;

    localparam M_IDLE = 0, M_IDADDR = 1, M_ID = 2, M_SIG = 3, M_PADDR = 4, M_PARAM = 5,
               M_RADDR = 6, M_DATA = 7, M_PGADDR = 8, M_PGDATA = 9, M_ERADDR = 10,
               M_STATUS = 11, M_RNDADDR = 12, M_PGRNDADDR = 13;

    reg [7:0] mem   [0:SIZE-1];
    reg [7:0] preg  [0:PB-1];
    reg [7:0] param [0:255];
    reg [7:0] addr  [0:7];

    reg        busy;
    reg        drive;
    reg [7:0]  dout;
    reg        fail;
    reg        resume;
    integer    mode, prev_mode, naddr, col, row, ptr, i, j, k;
    integer    violations;
    integer    programs, erases;
    real       t_we_fall, t_we_rise, t_io, t_ctl, t_addr_rise;
    real       t_re_fall, t_ce_fall;
    reg        first_data;

    assign rb_n = busy ? 1'b0 : 1'bz;
    assign io   = drive ? dout : 8'bz;

    // ---------------------------------------------------------------- init
    reg [15:0] crc;
    reg        fb;
    initial begin
        busy = 0; drive = 0; fail = 0; mode = M_IDLE; prev_mode = M_IDLE; resume = 0;
        violations = 0; programs = 0; erases = 0; naddr = 0; ptr = 0; first_data = 0;
        t_we_fall = 0; t_we_rise = -1000; t_io = 0; t_ctl = 0; t_addr_rise = -1000;
        t_re_fall = -1000; t_ce_fall = -1000;
        for (i = 0; i < SIZE; i = i + 1) mem[i] = 8'hFF;
        if (BAD_BLOCK >= 0 && BAD_BLOCK < BLOCKS)
            mem[BAD_BLOCK * PPB * PB + PAGE] = 8'h00;

        for (i = 0; i < 256; i = i + 1) param[i] = 8'h00;
        param[0] = "O"; param[1] = "N"; param[2] = "F"; param[3] = "I";
        param[4] = 8'h02;
        for (i = 32; i < 64; i = i + 1) param[i] = " ";
        param[32] = "N"; param[33] = "S"; param[34] = "P"; param[35] = "R"; param[36] = "O"; param[37] = "G";
        param[44] = "R"; param[45] = "T"; param[46] = "L"; param[47] = "-"; param[48] = "N";
        param[49] = "A"; param[50] = "N"; param[51] = "D";
        param[64] = ID[7:0];
        {param[83], param[82], param[81], param[80]} = PAGE;
        {param[85], param[84]} = SPARE;
        {param[95], param[94], param[93], param[92]} = PPB;
        {param[99], param[98], param[97], param[96]} = BLOCKS;
        param[100] = 1;
        param[101] = (COLC << 4) | ROWC;
        param[102] = 1;
        param[112] = 1;
        param[129] = 1;
        crc = 16'h4F4E;
        for (i = 0; i < 254; i = i + 1)
            for (j = 7; j >= 0; j = j - 1) begin
                fb  = crc[15] ^ param[i][j];
                crc = {crc[14:0], 1'b0};
                if (fb) crc = crc ^ 16'h8005;
            end
        param[254] = crc[7:0];
        param[255] = crc[15:8];
    end

    task violation(input [8*24-1:0] what);
        begin
            violations = violations + 1;
            $display("[nand_model] %0t ns: timing/protocol violation: %0s", $realtime, what);
        end
    endtask

    // ---------------------------------------------------------------- busy
    event ev_busy;
    real  busy_dur;
    always @(ev_busy) begin
        busy = 1;
        #(busy_dur);
        busy = 0;
    end
    task start_busy(input real d);
        begin
            busy_dur = d;
            -> ev_busy;
        end
    endtask

    // ---------------------------------------------------------------- timing bookkeeping
    always @(io)          if (!drive) t_io = $realtime;
    always @(cle or ale)  t_ctl = $realtime;
    always @(negedge we_n) if (!ce_n) t_we_fall = $realtime;

    function [31:0] get_col;
        input dummy;
        integer n;
        begin
            get_col = 0;
            for (n = 0; n < COLC; n = n + 1) get_col = get_col | (addr[n] << (8 * n));
        end
    endfunction

    function [31:0] get_row;
        input integer base;
        integer n;
        begin
            get_row = 0;
            for (n = 0; n < ROWC; n = n + 1) get_row = get_row | (addr[base + n] << (8 * n));
        end
    endfunction

    // ---------------------------------------------------------------- write cycles
    always @(posedge we_n) if (!ce_n) begin
        if ($realtime - t_we_fall < 15.0) violation("tWP");
        if ($realtime - t_io < 10.0)      violation("tDS");
        if ($realtime - t_ctl < 10.0)     violation("tCLS/tALS");
        if (cle && ale)                   violation("CLE and ALE both high");
        if (cle) begin
            if (busy && io != 8'h70 && io != 8'hFF) violation("command while busy");
            do_cmd(io);
        end else if (ale) begin
            if (busy) violation("address while busy");
            do_addr(io);
            t_addr_rise = $realtime;
            first_data  = 1;
        end else begin
            if (busy) violation("data in while busy");
            if (first_data && $realtime - t_addr_rise < 70.0) violation("tADL");
            first_data = 0;
            if (mode == M_PGDATA && col < PB) begin
                preg[col] = io;
                col = col + 1;
            end
        end
        t_we_rise = $realtime;
    end

    task do_cmd(input [7:0] c);
        begin
            case (c)
                8'hFF: begin mode = M_IDLE; fail = 0; start_busy(T_RST); end
                8'h90: mode = M_IDADDR;
                8'hEC: mode = M_PADDR;
                8'h00: begin
                    resume = (mode == M_STATUS) && (prev_mode == M_DATA);
                    mode = M_RADDR; naddr = 0;
                end
                8'h30: if (mode == M_RADDR) begin
                    col = get_col(0); row = get_row(COLC);
                    for (k = 0; k < PB; k = k + 1)
                        preg[k] = (row < PPB * BLOCKS) ? mem[row * PB + k] : 8'hFF;
                    start_busy(T_R);
                    mode = M_DATA;
                end
                8'h05: begin mode = M_RNDADDR; naddr = 0; end
                8'hE0: if (mode == M_RNDADDR) begin col = get_col(0); mode = M_DATA; end
                8'h80: begin
                    mode = M_PGADDR; naddr = 0;
                    for (k = 0; k < PB; k = k + 1) preg[k] = 8'hFF;
                end
                8'h85: begin mode = M_PGRNDADDR; naddr = 0; end
                8'h10: if (mode == M_PGDATA) begin
                    fail = 0;
                    if (wp_n && row < PPB * BLOCKS) begin
                        for (k = 0; k < PB; k = k + 1)
                            mem[row * PB + k] = mem[row * PB + k] & preg[k];
                        programs = programs + 1;
                    end
                    start_busy(T_PROG);
                    mode = M_IDLE;
                end
                8'h60: begin mode = M_ERADDR; naddr = 0; end
                8'hD0: if (mode == M_ERADDR) begin
                    row = get_row(0);
                    fail = 0;
                    if (wp_n && row < PPB * BLOCKS) begin
                        for (k = (row / PPB) * PPB * PB; k < ((row / PPB) + 1) * PPB * PB; k = k + 1)
                            mem[k] = 8'hFF;
                        erases = erases + 1;
                    end
                    start_busy(T_BERS);
                    mode = M_IDLE;
                end
                8'h70: begin prev_mode = mode; mode = M_STATUS; end
                default: violation("unknown command");
            endcase
        end
    endtask

    task do_addr(input [7:0] a);
        begin
            case (mode)
                M_IDADDR: begin
                    ptr  = 0;
                    mode = (a == 8'h20) ? M_SIG : M_ID;
                end
                M_PADDR: begin
                    ptr  = 0;
                    mode = M_PARAM;
                    start_busy(T_R);
                end
                M_RADDR, M_ERADDR, M_RNDADDR: begin
                    addr[naddr] = a;
                    naddr = naddr + 1;
                end
                M_PGADDR: begin
                    addr[naddr] = a;
                    naddr = naddr + 1;
                    if (naddr == COLC + ROWC) begin
                        col  = get_col(0);
                        row  = get_row(COLC);
                        mode = M_PGDATA;
                    end
                end
                M_PGRNDADDR: begin
                    addr[naddr] = a;
                    naddr = naddr + 1;
                    if (naddr == COLC) begin
                        col  = get_col(0);
                        mode = M_PGDATA;
                    end
                end
                default: violation("unexpected address");
            endcase
        end
    endtask

    // ---------------------------------------------------------------- read cycles
    task next_out;
        begin
            if (mode == M_RADDR && naddr == 0 && resume)
                mode = M_DATA;
            case (mode)
                M_ID:     begin dout = ID >> (8 * (ptr % 5)); ptr = ptr + 1; end
                M_SIG:    begin
                    case (ptr)
                        0: dout = "O"; 1: dout = "N"; 2: dout = "F"; 3: dout = "I";
                        default: dout = 8'h00;
                    endcase
                    ptr = ptr + 1;
                end
                M_PARAM:  begin dout = param[ptr % 256]; ptr = ptr + 1; end
                M_DATA:   begin dout = (col < PB) ? preg[col] : 8'hFF; col = col + 1; end
                M_STATUS: dout = {wp_n, ~busy, ~busy, 4'b0000, fail};
                default:  dout = 8'hFF;
            endcase
        end
    endtask

    always @(negedge re_n) if (!ce_n) begin
        if ($realtime - t_we_rise < 60.0) violation("tWHR");
        if (busy && mode != M_STATUS) violation("read while busy");
        #(T_REA);
        next_out;
        drive = 1;
    end

    always @(posedge re_n) begin
        #(5.0);
        drive = 0;
    end

    // Strobe glitches (e.g. from pad muxing) would issue spurious reads/commands on a real chip.
    always @(negedge re_n) t_re_fall = $realtime;
    always @(negedge ce_n) t_ce_fall = $realtime;
    always @(posedge re_n) if (!ce_n && $realtime - t_re_fall < 10.0) violation("RE# glitch (tRP)");
    always @(posedge ce_n) begin
        if ($realtime > 100.0 && $realtime - t_ce_fall < 10.0) violation("CE# glitch");
        drive = 0;
    end
endmodule
