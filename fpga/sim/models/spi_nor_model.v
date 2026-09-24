// Behavioural SPI NOR (W25Q-style, mode 0) with SFDP.
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
    input  wire mosi,
    output wire miso,
    input  wire wp_n,
    input  wire hold_n
);
    reg [7:0] mem  [0:SIZE-1];
    reg [7:0] sfdp [0:255];
    reg [7:0] pbuf [0:255];

    reg        busy, wel;
    reg [7:0]  sr1, sr2;
    reg [7:0]  in_sh, out_sh, nxt, cmd;
    reg [31:0] addr;
    reg        miso_r;
    integer    bitcnt, bytecnt, pn, i, k, violations;
    integer    programs, erases;

    assign miso = cs_n ? 1'bz : miso_r;

    task put32(input integer off, input [31:0] v);
        begin
            sfdp[off] = v[7:0]; sfdp[off + 1] = v[15:8];
            sfdp[off + 2] = v[23:16]; sfdp[off + 3] = v[31:24];
        end
    endtask

    initial begin
        busy = 0; wel = 0; sr1 = 8'h1C; sr2 = 8'h02; violations = 0; programs = 0; erases = 0;
        miso_r = 1'b1;
        for (i = 0; i < SIZE; i = i + 1) mem[i] = 8'hFF;
        for (i = 0; i < 256; i = i + 1) sfdp[i] = 8'hFF;
        sfdp[0] = "S"; sfdp[1] = "F"; sfdp[2] = "D"; sfdp[3] = "P";
        sfdp[4] = 8'h06; sfdp[5] = 8'h01; sfdp[6] = 8'h00; sfdp[7] = 8'hFF;
        sfdp[8] = 8'h00; sfdp[9] = 8'h06; sfdp[10] = 8'h01; sfdp[11] = 8'd16;
        sfdp[12] = 8'h30; sfdp[13] = 8'h00; sfdp[14] = 8'h00; sfdp[15] = 8'hFF;
        for (i = 0; i < 16; i = i + 1) put32(8'h30 + 4 * i, 32'hFFFFFFFF);
        put32(8'h30, 32'hFF00_20E1);
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

    // ---------------------------------------------------------------- shift logic
    always @(negedge cs_n) begin
        bitcnt = 0; bytecnt = 0; nxt = 8'hFF; pn = 0;
        if (!hold_n) begin
            violations = violations + 1;
            $display("[spi_nor_model] HOLD# low while selected");
        end
    end

    always @(posedge sck) if (!cs_n) begin
        in_sh  = {in_sh[6:0], mosi};
        bitcnt = bitcnt + 1;
        if (bitcnt == 8) begin
            bitcnt = 0;
            handle(in_sh);
            bytecnt = bytecnt + 1;
        end
    end

    always @(negedge sck) if (!cs_n) begin
        if (bitcnt == 0)
            out_sh = nxt;
        else
            out_sh = {out_sh[6:0], 1'b1};
        #(T_V) miso_r = out_sh[7];
    end

    task handle(input [7:0] b);
        begin
            if (bytecnt == 0) begin
                cmd = b;
                addr = 0;
                case (b)
                    8'h05: nxt = status1(0);
                    8'h35: nxt = sr2;
                    8'h9F: nxt = busy ? 8'hFF : JEDEC[23:16];
                    default: nxt = 8'hFF;
                endcase
            end else begin
                case (cmd)
                    8'h05: nxt = status1(0);
                    8'h35: nxt = sr2;
                    8'h9F: nxt = (bytecnt == 1) ? JEDEC[15:8] : (bytecnt == 2) ? JEDEC[7:0] : 8'hFF;
                    8'h03, 8'h0B, 8'h5A: begin
                        if (bytecnt <= 3) addr = {addr[23:0], b};
                        k = (cmd == 8'h03) ? 3 : 4;         // bytes before data
                        if (bytecnt >= k) begin
                            if (cmd == 8'h5A)
                                nxt = sfdp[(addr + bytecnt - k) % 256];
                            else
                                nxt = busy ? 8'hFF : mem[(addr + bytecnt - k) % SIZE];
                        end
                    end
                    8'h02: begin
                        if (bytecnt <= 3) addr = {addr[23:0], b};
                        else if (pn < 256) begin pbuf[pn] = b; pn = pn + 1; end
                    end
                    8'h20, 8'h52, 8'hD8: if (bytecnt <= 3) addr = {addr[23:0], b};
                    8'h01: begin
                        if (bytecnt == 1) pbuf[0] = b;
                        if (bytecnt == 2) pbuf[1] = b;
                        pn = bytecnt;
                    end
                    default: ;
                endcase
            end
        end
    endtask

    // ---------------------------------------------------------------- execute on CS# rise
    integer sz, base;
    always @(posedge cs_n) begin
        if (bitcnt != 0 && bytecnt > 0) begin
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
                8'h02: if (wel && bytecnt > 4) begin
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
