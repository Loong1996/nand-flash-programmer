// Behavioural SPI NAND (W25N01GV-style, buffer read mode, mode 0).
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
    input  wire mosi,
    output wire miso,
    input  wire wp_n,
    input  wire hold_n
);
    localparam PB   = PAGE + SPARE;
    localparam SIZE = PPB * BLOCKS * PB;

    reg [7:0] mem   [0:SIZE-1];
    reg [7:0] cache [0:PB-1];

    reg        busy;
    reg [7:0]  prot, cfg, st;
    reg [7:0]  in_sh, out_sh, nxt, cmd, freg;
    reg [23:0] row;
    reg [15:0] col;
    reg        miso_r;
    integer    bitcnt, bytecnt, i, violations, dcol;

    // DO stays high-Z until the command byte has been received (like real parts)
    assign miso = (cs_n || bytecnt == 0) ? 1'bz : miso_r;

    initial begin
        busy = 0; prot = 8'h7C; cfg = 8'h18; st = 8'h00; violations = 0; miso_r = 1'b1;
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

    always @(negedge cs_n) begin
        bitcnt = 0; bytecnt = 0; nxt = 8'hFF;
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
                row = 0;
                col = 0;
                nxt = 8'hFF;
                if ((busy && b != 8'h0F) || b == 8'h00) ;
            end else begin
                case (cmd)
                    8'h9F: nxt = (bytecnt == 1) ? ID[23:16] : (bytecnt == 2) ? ID[15:8] :
                                 (bytecnt == 3) ? ID[7:0] : 8'hFF;
                    8'h0F: begin
                        if (bytecnt == 1) freg = b;
                        nxt = (freg == 8'hC0) ? status(0) : (freg == 8'hA0) ? prot :
                              (freg == 8'hB0) ? cfg : 8'h00;
                    end
                    8'h1F: begin
                        if (bytecnt == 1) freg = b;
                        if (bytecnt == 2) begin
                            if (freg == 8'hA0) prot = b;
                            if (freg == 8'hB0) cfg = b;
                        end
                    end
                    8'h13, 8'h10, 8'hD8: if (bytecnt <= 3) row = {row[15:0], b};
                    8'h03, 8'h0B: begin
                        if (bytecnt <= 2) col = {col[7:0], b};
                        if (bytecnt >= 3) begin
                            if (busy) begin
                                violations = violations + 1;
                                $display("[spi_nand_model] cache read while busy");
                            end
                            dcol = (col & 16'h0FFF) + bytecnt - 3;
                            nxt = cache[dcol % PB];
                        end
                    end
                    8'h02, 8'h84: begin
                        if (bytecnt <= 2) col = {col[7:0], b};
                        else begin
                            dcol = (col & 16'h0FFF) + bytecnt - 3;
                            if (dcol < PB) cache[dcol] = b;
                        end
                    end
                    default: ;
                endcase
                if (cmd == 8'h02 && bytecnt == 1)
                    for (i = 0; i < PB; i = i + 1) cache[i] = 8'hFF;
            end
        end
    endtask

    always @(posedge cs_n) begin
        if (bitcnt != 0 && bytecnt > 0) begin
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
