// Behavioural SPI NAND (W25N01GV-style, buffer read mode, mode 0) with dual / quad commands:
//   03h/0Bh read (x1), 3Bh (x2 data), 6Bh (x4 data), BBh (x2 column + 4 dummy clocks, x2 data),
//   EBh (x4 column + 4 dummy clocks, x4 data), 02h/84h program load (x1), 32h/34h (x4),
//   13h page read, 10h program execute, D8h erase, 0Fh/1Fh features, 9Fh ID, 06h/04h, FFh.
`timescale 1ns/1ps

module spi_nand_model #(
    parameter PAGE      = 2048,
    parameter SPARE     = 64,
    parameter PPB       = 64,
    parameter BLOCKS    = 4,
    parameter [23:0] ID = 24'hEFAA21,
    parameter BAD_BLOCK = 2,
    parameter real T_RD = 4000.0,
    parameter real T_PP = 8000.0,
    parameter real T_BE = 12000.0,
    parameter real T_V  = 7.0
) (
    input  wire cs_n,
    input  wire sck,
    inout  wire mosi,      // IO0
    inout  wire miso,      // IO1
    inout  wire wp_n,      // IO2
    inout  wire hold_n     // IO3
);
    localparam PB   = PAGE + SPARE;
    localparam SIZE = PPB * BLOCKS * PB;

    reg [7:0] mem   [0:SIZE-1];
    reg [7:0] cache [0:PB-1];

    reg        busy;
    reg [7:0]  prot, cfg, st;
    reg [7:0]  in_sh, cmd, freg;
    reg [23:0] row;
    reg [15:0] col;
    integer    i, violations, dcol;

    // phase plan (after the command byte)
    integer    n_addr, n_dummy, w_in, w_out, w_data_in;
    reg        data_out;
    // progress
    integer    bitcnt, bytecnt, dummy_left, out_bits, didx, pidx;
    reg        in_data, driving;
    reg  [7:0] out_sh;
    reg  [3:0] io_o;

    wire [3:0] io_i = {hold_n, wp_n, miso, mosi};

    // DO stays high-Z until the command byte has been received (like real parts)
    assign mosi   = (!cs_n && driving && w_out >= 2) ? io_o[0] : 1'bz;
    assign miso   = (!cs_n && driving) ? io_o[1] : 1'bz;
    assign wp_n   = (!cs_n && driving && w_out == 4) ? io_o[2] : 1'bz;
    assign hold_n = (!cs_n && driving && w_out == 4) ? io_o[3] : 1'bz;

    initial begin
        busy = 0; prot = 8'h7C; cfg = 8'h18; st = 8'h00; violations = 0; driving = 0; io_o = 4'hF;
        for (i = 0; i < SIZE; i = i + 1) mem[i] = 8'hFF;
        for (i = 0; i < PB; i = i + 1) cache[i] = 8'hFF;
        if (BAD_BLOCK >= 0 && BAD_BLOCK < BLOCKS)
            mem[BAD_BLOCK * PPB * PB + PAGE] = 8'h00;
    end

    event ev_busy;
    real  busy_dur;
    always @(ev_busy) begin
        busy = 1;
        #(busy_dur);
        busy = 0;
    end

    function [7:0] status;
        input dummy;
        status = st | (busy ? 8'h01 : 8'h00);
    endfunction

    function [7:0] data_byte;
        input integer idx;
        begin
            case (cmd)
                8'h9F: data_byte = (idx == 0) ? ID[23:16] : (idx == 1) ? ID[15:8] : (idx == 2) ? ID[7:0] : 8'hFF;
                8'h0F: data_byte = (freg == 8'hC0) ? status(0) : (freg == 8'hA0) ? prot :
                                   (freg == 8'hB0) ? cfg : 8'h00;
                default: begin
                    dcol = (col & 16'h0FFF) + idx;
                    data_byte = cache[dcol % PB];
                end
            endcase
        end
    endfunction

    task plan(input [7:0] c);
        begin
            cmd = c; row = 0; col = 0; n_addr = 0; n_dummy = 0; w_in = 1; w_out = 1; w_data_in = 1;
            data_out = 0; pidx = 0;
            case (c)
                8'h9F:        begin n_addr = 1; data_out = 1; end          // one dummy byte
                8'h0F:        begin n_addr = 1; data_out = 1; end          // feature address
                8'h1F:        n_addr = 1;
                8'h13, 8'h10, 8'hD8: n_addr = 3;
                8'h03, 8'h0B: begin n_addr = 2; n_dummy = 8; data_out = 1; end
                8'h3B:        begin n_addr = 2; n_dummy = 8; data_out = 1; w_out = 2; end
                8'h6B:        begin n_addr = 2; n_dummy = 8; data_out = 1; w_out = 4; end
                8'hBB:        begin n_addr = 2; w_in = 2; n_dummy = 4; data_out = 1; w_out = 2; end
                8'hEB:        begin n_addr = 2; w_in = 4; n_dummy = 4; data_out = 1; w_out = 4; end
                8'h02, 8'h84: n_addr = 2;
                8'h32, 8'h34: begin n_addr = 2; w_data_in = 4; end
                default: ;
            endcase
            if (c == 8'h02 || c == 8'h32)
                for (i = 0; i < PB; i = i + 1) cache[i] = 8'hFF;
        end
    endtask

    always @(negedge cs_n) begin
        bitcnt = 0; bytecnt = 0; in_data = 0; driving = 0; dummy_left = 0; out_bits = 0; didx = 0;
        cmd = 8'h00; w_in = 1; w_out = 1; w_data_in = 1; data_out = 0; n_addr = 0; n_dummy = 0;
    end

    function integer cur_w;
        input dummy;
        begin
            if (bytecnt == 0) cur_w = 1;
            else if (bytecnt > n_addr) cur_w = w_data_in;
            else cur_w = w_in;
        end
    endfunction

    task start_out;
        begin
            if (data_out) begin
                if (busy && cmd != 8'h0F && cmd != 8'h9F) begin
                    violations = violations + 1;
                    $display("[spi_nand_model] cache read while busy");
                end
                driving = 1;
                out_sh = data_byte(didx);
                out_bits = 8;
            end
        end
    endtask

    always @(posedge sck) if (!cs_n) begin
        if (in_data && data_out) begin
            ;
        end else if (dummy_left > 0) begin
            dummy_left = dummy_left - 1;
            if (dummy_left == 0) begin in_data = 1; start_out; end
        end else begin
            if (cur_w(0) == 4)      in_sh = {in_sh[3:0], io_i};
            else if (cur_w(0) == 2) in_sh = {in_sh[5:0], io_i[1:0]};
            else                    in_sh = {in_sh[6:0], io_i[0]};
            bitcnt = bitcnt + cur_w(0);
            if (bitcnt >= 8) begin
                bitcnt = 0;
                take(in_sh);
                bytecnt = bytecnt + 1;
            end
        end
    end

    task take(input [7:0] b);
        begin
            if (bytecnt == 0) begin
                plan(b);
            end else if (bytecnt <= n_addr) begin
                if (cmd == 8'h0F || cmd == 8'h1F) freg = b;
                else if (cmd == 8'h13 || cmd == 8'h10 || cmd == 8'hD8) row = {row[15:0], b};
                else col = {col[7:0], b};
                if (bytecnt == n_addr && data_out) begin
                    if (n_dummy > 0) dummy_left = n_dummy;
                    else begin in_data = 1; start_out; end
                end
            end else begin
                if (cmd == 8'h1F && bytecnt == 2) begin
                    if (freg == 8'hA0) prot = b;
                    if (freg == 8'hB0) cfg = b;
                end
                if (cmd == 8'h02 || cmd == 8'h84 || cmd == 8'h32 || cmd == 8'h34) begin
                    dcol = (col & 16'h0FFF) + pidx;
                    if (dcol < PB) cache[dcol] = b;
                    pidx = pidx + 1;
                end
            end
        end
    endtask

    always @(negedge sck) if (!cs_n && driving) begin
        if (out_bits == 0) begin
            didx = didx + 1;
            out_sh = data_byte(didx);
            out_bits = 8;
        end
        #(T_V);
        if (w_out == 4) begin
            io_o = out_sh[7:4]; out_sh = {out_sh[3:0], 4'h0}; out_bits = out_bits - 4;
        end else if (w_out == 2) begin
            io_o = {2'b11, out_sh[7:6]}; out_sh = {out_sh[5:0], 2'b00}; out_bits = out_bits - 2;
        end else begin
            io_o = {2'b11, out_sh[7], 1'b1}; out_sh = {out_sh[6:0], 1'b1}; out_bits = out_bits - 1;
        end
    end

    always @(posedge cs_n) begin
        driving = 0;
        if (bitcnt != 0 && bytecnt > 0 && !(in_data && data_out)) begin
            violations = violations + 1;
            $display("[spi_nand_model] CS# raised mid-byte (cmd %02x)", cmd);
        end
        if (bytecnt > 0 && !busy) begin
            case (cmd)
                8'hFF: begin st = 0; busy_dur = 1000.0; -> ev_busy; end
                8'h06: st = st | 8'h02;
                8'h04: st = st & ~8'h02;
                8'h13: if (bytecnt >= 4) begin
                    for (i = 0; i < PB; i = i + 1)
                        cache[i] = (row < PPB * BLOCKS) ? mem[row * PB + i] : 8'hFF;
                    busy_dur = T_RD; -> ev_busy;
                end
                8'h10: if (bytecnt >= 4) begin
                    st = st & ~8'h08;
                    if ((st & 8'h02) && (prot & 8'h78) == 0 && row < PPB * BLOCKS) begin
                        for (i = 0; i < PB; i = i + 1)
                            mem[row * PB + i] = mem[row * PB + i] & cache[i];
                    end else begin
                        st = st | 8'h08;
                    end
                    st = st & ~8'h02;
                    busy_dur = T_PP; -> ev_busy;
                end
                8'hD8: if (bytecnt >= 4) begin
                    st = st & ~8'h04;
                    if ((st & 8'h02) && (prot & 8'h78) == 0 && row < PPB * BLOCKS) begin
                        for (i = (row / PPB) * PPB * PB; i < ((row / PPB) + 1) * PPB * PB; i = i + 1)
                            mem[i] = 8'hFF;
                    end else begin
                        st = st | 8'h04;
                    end
                    st = st & ~8'h02;
                    busy_dur = T_BE; -> ev_busy;
                end
                default: ;
            endcase
        end
    end
endmodule
