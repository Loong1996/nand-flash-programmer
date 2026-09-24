// Behavioural SPI NOR (W25Q-style, mode 0) with SFDP and dual / quad commands:
//   03h read, 0Bh fast read, 3Bh 1-1-2, 6Bh 1-1-4, BBh 1-2-2, EBh 1-4-4,
//   02h page program, 32h quad page program (1-1-4), 20h/52h/D8h erase, C7h/60h,
//   05h/35h status, 01h write status, 06h/04h, 9Fh JEDEC ID, 5Ah SFDP.
// Every phase after the command byte has its own bus width; quad commands need
// QE (SR2 bit 1) set, otherwise they count as a violation.
`timescale 1ns/1ps

module spi_nor_model #(
    parameter SIZE          = 2 * 1024 * 1024,
    parameter [23:0] JEDEC  = 24'hEF4015,
    parameter real T_PP     = 20000.0,
    parameter real T_SE     = 30000.0,
    parameter real T_BE     = 50000.0,
    parameter real T_CE     = 80000.0,
    parameter real T_V      = 7.0
) (
    input  wire cs_n,
    input  wire sck,
    inout  wire mosi,      // IO0
    inout  wire miso,      // IO1
    inout  wire wp_n,      // IO2
    inout  wire hold_n     // IO3
);
    reg [7:0] mem  [0:SIZE-1];
    reg [7:0] sfdp [0:255];
    reg [7:0] pbuf [0:255];

    reg        busy, wel;
    reg [7:0]  sr1, sr2;
    reg [7:0]  in_sh, nxt, cmd;
    reg [31:0] addr;
    integer    violations, programs, erases;

    // ---- per-command phase plan (set after the command byte)
    integer    n_addr;        // address bytes
    integer    n_mode;        // mode bytes (sent by the host, ignored)
    integer    n_dummy;       // dummy clocks after address + mode
    integer    w_in;          // bits per clock while the host sends (after the command byte)
    integer    w_out;         // bits per clock while the chip sends
    reg        data_out;      // data phase: chip drives
    // ---- progress
    integer    bitcnt;        // bits of the current input byte
    integer    bytecnt;       // bytes received so far (command = byte 0)
    integer    dummy_left;
    reg        in_data;       // past address/mode/dummy
    reg        driving;       // chip drives IO lines
    reg  [7:0] out_sh;
    integer    out_bits;      // bits left in out_sh
    integer    didx;          // data byte index
    integer    pn, i, k;
    reg  [3:0] io_o;          // {IO3, IO2, IO1, IO0} values while driving

    wire [3:0] io_i = {hold_n, wp_n, miso, mosi};

    // DO stays high-Z until the command byte has been received (like real parts)
    assign mosi   = (!cs_n && driving && w_out >= 2) ? io_o[0] : 1'bz;
    assign miso   = (!cs_n && driving) ? io_o[1] : 1'bz;
    assign wp_n   = (!cs_n && driving && w_out == 4) ? io_o[2] : 1'bz;
    assign hold_n = (!cs_n && driving && w_out == 4) ? io_o[3] : 1'bz;

    task put32(input integer off, input [31:0] v);
        begin
            sfdp[off] = v[7:0]; sfdp[off + 1] = v[15:8];
            sfdp[off + 2] = v[23:16]; sfdp[off + 3] = v[31:24];
        end
    endtask

    initial begin
        busy = 0; wel = 0; sr1 = 8'h1C; sr2 = 8'h02; violations = 0; programs = 0; erases = 0;
        driving = 0; io_o = 4'hF;
        for (i = 0; i < SIZE; i = i + 1) mem[i] = 8'hFF;
        for (i = 0; i < 256; i = i + 1) sfdp[i] = 8'hFF;
        sfdp[0] = "S"; sfdp[1] = "F"; sfdp[2] = "D"; sfdp[3] = "P";
        sfdp[4] = 8'h06; sfdp[5] = 8'h01; sfdp[6] = 8'h00; sfdp[7] = 8'hFF;
        sfdp[8] = 8'h00; sfdp[9] = 8'h06; sfdp[10] = 8'h01; sfdp[11] = 8'd16;
        sfdp[12] = 8'h30; sfdp[13] = 8'h00; sfdp[14] = 8'h00; sfdp[15] = 8'hFF;
        for (i = 0; i < 16; i = i + 1) put32(8'h30 + 4 * i, 32'hFFFFFFFF);
        put32(8'h30, 32'hFF7120E1);                       // 1-1-2, 1-2-2, 1-4-4, 1-1-4 fast reads
        put32(8'h30 + 8, 32'h6B08_EB44);                  // 1-1-4: 6Bh 8 dummy; 1-4-4: EBh 2 mode + 4 dummy
        put32(8'h30 + 12, 32'hBB80_3B08);                 // 1-2-2: BBh 4 mode clocks; 1-1-2: 3Bh 8 dummy
        put32(8'h30 + 56, 32'hFFDF_FFFF);                 // DWORD15: QER = 101b (QE = SR2 bit 1)
        put32(8'h34, SIZE * 8 - 1);
        put32(8'h30 + 28, {8'h52, 8'd15, 8'h20, 8'd12});
        put32(8'h30 + 32, {8'h00, 8'h00, 8'hD8, 8'd16});
        put32(8'h30 + 40, 32'hFFFFFF81);
    end

    // ---------------------------------------------------------------- busy
    event ev_busy;
    real  busy_dur;
    always @(ev_busy) begin
        busy = 1;
        #(busy_dur);
        busy = 0;
    end

    function [7:0] status1;
        input dummy;
        status1 = sr1 | (wel ? 8'h02 : 8'h00) | (busy ? 8'h01 : 8'h00);
    endfunction

    function [7:0] data_byte;
        input integer idx;
        begin
            if (cmd == 8'h5A)
                data_byte = sfdp[(addr + idx) % 256];
            else if (cmd == 8'h05)
                data_byte = status1(0);
            else if (cmd == 8'h35)
                data_byte = sr2;
            else if (cmd == 8'h9F)
                data_byte = (idx == 0) ? JEDEC[23:16] : (idx == 1) ? JEDEC[15:8] : (idx == 2) ? JEDEC[7:0] : 8'hFF;
            else
                data_byte = busy ? 8'hFF : mem[(addr + idx) % SIZE];
        end
    endfunction

    task quad_needs_qe;
        begin
            if (!sr2[1]) begin
                violations = violations + 1;
                $display("[spi_nor_model] quad command %02x with QE=0", cmd);
            end
        end
    endtask

    // ---------------------------------------------------------------- command decode
    task plan(input [7:0] c);
        begin
            cmd = c; addr = 0; n_addr = 0; n_mode = 0; n_dummy = 0; w_in = 1; w_out = 1;
            data_out = 0; pn = 0;
            case (c)
                8'h03:              begin n_addr = 3; data_out = 1; end
                8'h0B, 8'h5A:       begin n_addr = 3; n_dummy = 8; data_out = 1; end
                8'h3B:              begin n_addr = 3; n_dummy = 8; data_out = 1; w_out = 2; end
                8'h6B:              begin n_addr = 3; n_dummy = 8; data_out = 1; w_out = 4; quad_needs_qe; end
                8'hBB:              begin n_addr = 3; n_mode = 1; w_in = 2; data_out = 1; w_out = 2; end
                8'hEB:              begin n_addr = 3; n_mode = 1; n_dummy = 4; w_in = 4; data_out = 1; w_out = 4;
                                          quad_needs_qe; end
                8'h02:              begin n_addr = 3; end
                8'h32:              begin n_addr = 3; quad_needs_qe; end     // data phase x4 (see below)
                8'h20, 8'h52, 8'hD8: n_addr = 3;
                8'h05, 8'h35, 8'h9F: data_out = 1;
                default: ;
            endcase
        end
    endtask

    // ---------------------------------------------------------------- shift logic
    always @(negedge cs_n) begin
        bitcnt = 0; bytecnt = 0; in_data = 0; driving = 0; dummy_left = 0; out_bits = 0; didx = 0;
        cmd = 8'h00; w_in = 1; w_out = 1; data_out = 0; n_addr = 0; n_mode = 0; n_dummy = 0;
        if (!hold_n) begin
            violations = violations + 1;
            $display("[spi_nor_model] HOLD# low while selected");
        end
    end

    // current input width: the command byte is always x1; 32h sends data x4
    function integer cur_w_in;
        input dummy;
        begin
            if (bytecnt == 0) cur_w_in = 1;
            else if (cmd == 8'h32 && bytecnt > n_addr) cur_w_in = 4;
            else cur_w_in = w_in;
        end
    endfunction

    always @(posedge sck) if (!cs_n) begin
        if (in_data && data_out) begin
            ;                                             // chip is sending
        end else if (dummy_left > 0) begin
            dummy_left = dummy_left - 1;
            if (dummy_left == 0) begin in_data = 1; start_out; end
        end else begin
            k = cur_w_in(0);
            if (k == 4)      in_sh = {in_sh[3:0], io_i};
            else if (k == 2) in_sh = {in_sh[5:0], io_i[1:0]};
            else             in_sh = {in_sh[6:0], io_i[0]};
            bitcnt = bitcnt + k;
            if (bitcnt >= 8) begin
                bitcnt = 0;
                take(in_sh);
                bytecnt = bytecnt + 1;
            end
        end
    end

    task start_out;
        begin
            if (data_out) begin
                driving = 1;
                out_sh = data_byte(didx);
                out_bits = 8;
            end
        end
    endtask

    task take(input [7:0] b);
        begin
            if (bytecnt == 0) begin
                plan(b);
                if (data_out && n_addr == 0) begin in_data = 1; start_out; end
            end else if (bytecnt <= n_addr) begin
                addr = {addr[23:0], b};
                if (bytecnt == n_addr && n_mode == 0) begin
                    if (n_dummy > 0) dummy_left = n_dummy;
                    else begin in_data = 1; start_out; end
                end
            end else if (bytecnt <= n_addr + n_mode) begin
                if (bytecnt == n_addr + n_mode) begin
                    if (n_dummy > 0) dummy_left = n_dummy;
                    else begin in_data = 1; start_out; end
                end
            end else begin
                // data from the host: program buffer / status register
                if (cmd == 8'h02 || cmd == 8'h32) begin
                    if (pn < 256) begin pbuf[pn] = b; pn = pn + 1; end
                end else if (cmd == 8'h01) begin
                    pbuf[pn] = b; pn = pn + 1;
                end
            end
            if (cmd == 8'h01 && bytecnt >= 1 && bytecnt <= 2) begin
                pbuf[bytecnt - 1] = b; pn = bytecnt;
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

    // ---------------------------------------------------------------- execute on CS# rise
    integer sz, base;
    always @(posedge cs_n) begin
        driving = 0;
        if (bitcnt != 0 && !(in_data && data_out)) begin
            violations = violations + 1;
            $display("[spi_nor_model] CS# raised mid-byte (cmd %02x)", cmd);
        end
        if (!busy && bytecnt > 0) begin
            case (cmd)
                8'h06: wel = 1;
                8'h04: wel = 0;
                8'h01: if (wel && pn >= 1) begin
                    if (!wp_n && sr1[7]) ;               // hardware protected
                    else begin
                        sr1 = pbuf[0] & 8'hFC;
                        if (pn >= 2) sr2 = pbuf[1];
                    end
                    wel = 0; busy_dur = 5000.0; -> ev_busy;
                end
                8'h02, 8'h32: if (wel && bytecnt > 4) begin
                    if ((sr1 & 8'h1C) == 0) begin
                        for (i = 0; i < pn; i = i + 1)
                            mem[(addr & ~32'hFF) | ((addr + i) & 32'hFF)] =
                                mem[(addr & ~32'hFF) | ((addr + i) & 32'hFF)] & pbuf[i];
                        programs = programs + 1;
                    end
                    wel = 0; busy_dur = T_PP; -> ev_busy;
                end
                8'h20, 8'h52, 8'hD8: if (wel && bytecnt >= 4) begin
                    sz = (cmd == 8'h20) ? 4096 : (cmd == 8'h52) ? 32768 : 65536;
                    base = addr & ~(sz - 1);
                    if ((sr1 & 8'h1C) == 0) begin
                        for (i = 0; i < sz; i = i + 1) mem[(base + i) % SIZE] = 8'hFF;
                        erases = erases + 1;
                    end
                    wel = 0; busy_dur = (cmd == 8'h20) ? T_SE : T_BE; -> ev_busy;
                end
                8'hC7, 8'h60: if (wel) begin
                    if ((sr1 & 8'h1C) == 0) begin
                        for (i = 0; i < SIZE; i = i + 1) mem[i] = 8'hFF;
                        erases = erases + 1;
                    end
                    wel = 0; busy_dur = T_CE; -> ev_busy;
                end
                default: ;
            endcase
        end
    end
endmodule
