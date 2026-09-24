// Simulation models of Gowin block RAMs for the gate-level simulation.
`timescale 1ns/1ps

// Behavioural model of the Gowin DPB block RAM (16384 bits, two ports).
// Only what nsprog's synthesised netlist uses is modelled faithfully:
// bypass (READ_MODE=0) or pipelined (1) output, normal (0) / write-through (1) /
// read-before-write (2) write modes, any port width listed below, BLKSEL match.
// INIT_RAM_* is accepted but ignored (memory starts as zeros).
module DPB #(
    parameter READ_MODE0 = 1'b0,
    parameter READ_MODE1 = 1'b0,
    parameter WRITE_MODE0 = 2'b00,
    parameter WRITE_MODE1 = 2'b00,
    parameter BIT_WIDTH_0 = 16,
    parameter BIT_WIDTH_1 = 16,
    parameter BLK_SEL_0 = 3'b000,
    parameter BLK_SEL_1 = 3'b000,
    parameter RESET_MODE = "SYNC",
    parameter [256-1:0] INIT_RAM_00 = 0,
    parameter [256-1:0] INIT_RAM_01 = 0,
    parameter [256-1:0] INIT_RAM_02 = 0,
    parameter [256-1:0] INIT_RAM_03 = 0,
    parameter [256-1:0] INIT_RAM_04 = 0,
    parameter [256-1:0] INIT_RAM_05 = 0,
    parameter [256-1:0] INIT_RAM_06 = 0,
    parameter [256-1:0] INIT_RAM_07 = 0,
    parameter [256-1:0] INIT_RAM_08 = 0,
    parameter [256-1:0] INIT_RAM_09 = 0,
    parameter [256-1:0] INIT_RAM_0A = 0,
    parameter [256-1:0] INIT_RAM_0B = 0,
    parameter [256-1:0] INIT_RAM_0C = 0,
    parameter [256-1:0] INIT_RAM_0D = 0,
    parameter [256-1:0] INIT_RAM_0E = 0,
    parameter [256-1:0] INIT_RAM_0F = 0,
    parameter [256-1:0] INIT_RAM_10 = 0,
    parameter [256-1:0] INIT_RAM_11 = 0,
    parameter [256-1:0] INIT_RAM_12 = 0,
    parameter [256-1:0] INIT_RAM_13 = 0,
    parameter [256-1:0] INIT_RAM_14 = 0,
    parameter [256-1:0] INIT_RAM_15 = 0,
    parameter [256-1:0] INIT_RAM_16 = 0,
    parameter [256-1:0] INIT_RAM_17 = 0,
    parameter [256-1:0] INIT_RAM_18 = 0,
    parameter [256-1:0] INIT_RAM_19 = 0,
    parameter [256-1:0] INIT_RAM_1A = 0,
    parameter [256-1:0] INIT_RAM_1B = 0,
    parameter [256-1:0] INIT_RAM_1C = 0,
    parameter [256-1:0] INIT_RAM_1D = 0,
    parameter [256-1:0] INIT_RAM_1E = 0,
    parameter [256-1:0] INIT_RAM_1F = 0,
    parameter [256-1:0] INIT_RAM_20 = 0,
    parameter [256-1:0] INIT_RAM_21 = 0,
    parameter [256-1:0] INIT_RAM_22 = 0,
    parameter [256-1:0] INIT_RAM_23 = 0,
    parameter [256-1:0] INIT_RAM_24 = 0,
    parameter [256-1:0] INIT_RAM_25 = 0,
    parameter [256-1:0] INIT_RAM_26 = 0,
    parameter [256-1:0] INIT_RAM_27 = 0,
    parameter [256-1:0] INIT_RAM_28 = 0,
    parameter [256-1:0] INIT_RAM_29 = 0,
    parameter [256-1:0] INIT_RAM_2A = 0,
    parameter [256-1:0] INIT_RAM_2B = 0,
    parameter [256-1:0] INIT_RAM_2C = 0,
    parameter [256-1:0] INIT_RAM_2D = 0,
    parameter [256-1:0] INIT_RAM_2E = 0,
    parameter [256-1:0] INIT_RAM_2F = 0,
    parameter [256-1:0] INIT_RAM_30 = 0,
    parameter [256-1:0] INIT_RAM_31 = 0,
    parameter [256-1:0] INIT_RAM_32 = 0,
    parameter [256-1:0] INIT_RAM_33 = 0,
    parameter [256-1:0] INIT_RAM_34 = 0,
    parameter [256-1:0] INIT_RAM_35 = 0,
    parameter [256-1:0] INIT_RAM_36 = 0,
    parameter [256-1:0] INIT_RAM_37 = 0,
    parameter [256-1:0] INIT_RAM_38 = 0,
    parameter [256-1:0] INIT_RAM_39 = 0,
    parameter [256-1:0] INIT_RAM_3A = 0,
    parameter [256-1:0] INIT_RAM_3B = 0,
    parameter [256-1:0] INIT_RAM_3C = 0,
    parameter [256-1:0] INIT_RAM_3D = 0,
    parameter [256-1:0] INIT_RAM_3E = 0,
    parameter [256-1:0] INIT_RAM_3F = 0,
    parameter UNUSED = 0
) (
    input  wire          CLKA, CEA, OCEA, RESETA, WREA,
    input  wire          CLKB, CEB, OCEB, RESETB, WREB,
    input  wire [13:0]   ADA, ADB,
    input  wire [2:0]    BLKSELA, BLKSELB,
    input  wire [15:0]   DIA, DIB,
    output wire [15:0]   DOA, DOB
);
    localparam SIZE = 16384;
    reg mem [0:SIZE-1];
    reg [15:0] ra, rb, pa, pb;
    integer i;
    initial begin
        for (i = 0; i < SIZE; i = i + 1) mem[i] = 1'b0;
        ra = 0; rb = 0; pa = 0; pb = 0;
    end

    function integer shift_of(input integer w);
        integer shift;
        begin
            shift = 0;
            case (w) 1: shift = 0; 2: shift = 1; 4: shift = 2; 8: shift = 3; 16: shift = 4; default: shift = 0; endcase
            shift_of = shift;
        end
    endfunction

    task automatic port(input integer w, input integer wmode, input [13:0] ad, input we,
                        input [15:0] di, inout [15:0] r);
        integer base, k;
        reg [15:0] old;
        begin
            base = (ad >> shift_of(w)) * w;
            old = 0;
            for (k = 0; k < w; k = k + 1) old[k] = mem[(base + k) % SIZE];
            if (we) begin
                for (k = 0; k < w; k = k + 1)
                    if (w < 16 || ad[k / (w / 2)])      // 16/18-bit: AD[1:0] byte enables
                        mem[(base + k) % SIZE] = di[k];
                if (wmode == 1) r = di;
                else if (wmode == 2) r = old;
            end else begin
                r = old;
            end
        end
    endtask

    always @(posedge CLKA) begin
        if (RESETA) begin ra <= 0; pa <= 0; end
        else begin
            if (CEA && BLKSELA == BLK_SEL_0) port(BIT_WIDTH_0, WRITE_MODE0, ADA, WREA, DIA, ra);
            if (OCEA) pa <= ra;
        end
    end
    always @(posedge CLKB) begin
        if (RESETB) begin rb <= 0; pb <= 0; end
        else begin
            if (CEB && BLKSELB == BLK_SEL_1) port(BIT_WIDTH_1, WRITE_MODE1, ADB, WREB, DIB, rb);
            if (OCEB) pb <= rb;
        end
    end
    assign DOA = READ_MODE0 ? pa : ra;
    assign DOB = READ_MODE1 ? pb : rb;
endmodule

// Behavioural model of the Gowin DPX9B block RAM (18432 bits, two ports).
// Only what nsprog's synthesised netlist uses is modelled faithfully:
// bypass (READ_MODE=0) or pipelined (1) output, normal (0) / write-through (1) /
// read-before-write (2) write modes, any port width listed below, BLKSEL match.
// INIT_RAM_* is accepted but ignored (memory starts as zeros).
module DPX9B #(
    parameter READ_MODE0 = 1'b0,
    parameter READ_MODE1 = 1'b0,
    parameter WRITE_MODE0 = 2'b00,
    parameter WRITE_MODE1 = 2'b00,
    parameter BIT_WIDTH_0 = 18,
    parameter BIT_WIDTH_1 = 18,
    parameter BLK_SEL_0 = 3'b000,
    parameter BLK_SEL_1 = 3'b000,
    parameter RESET_MODE = "SYNC",
    parameter [288-1:0] INIT_RAM_00 = 0,
    parameter [288-1:0] INIT_RAM_01 = 0,
    parameter [288-1:0] INIT_RAM_02 = 0,
    parameter [288-1:0] INIT_RAM_03 = 0,
    parameter [288-1:0] INIT_RAM_04 = 0,
    parameter [288-1:0] INIT_RAM_05 = 0,
    parameter [288-1:0] INIT_RAM_06 = 0,
    parameter [288-1:0] INIT_RAM_07 = 0,
    parameter [288-1:0] INIT_RAM_08 = 0,
    parameter [288-1:0] INIT_RAM_09 = 0,
    parameter [288-1:0] INIT_RAM_0A = 0,
    parameter [288-1:0] INIT_RAM_0B = 0,
    parameter [288-1:0] INIT_RAM_0C = 0,
    parameter [288-1:0] INIT_RAM_0D = 0,
    parameter [288-1:0] INIT_RAM_0E = 0,
    parameter [288-1:0] INIT_RAM_0F = 0,
    parameter [288-1:0] INIT_RAM_10 = 0,
    parameter [288-1:0] INIT_RAM_11 = 0,
    parameter [288-1:0] INIT_RAM_12 = 0,
    parameter [288-1:0] INIT_RAM_13 = 0,
    parameter [288-1:0] INIT_RAM_14 = 0,
    parameter [288-1:0] INIT_RAM_15 = 0,
    parameter [288-1:0] INIT_RAM_16 = 0,
    parameter [288-1:0] INIT_RAM_17 = 0,
    parameter [288-1:0] INIT_RAM_18 = 0,
    parameter [288-1:0] INIT_RAM_19 = 0,
    parameter [288-1:0] INIT_RAM_1A = 0,
    parameter [288-1:0] INIT_RAM_1B = 0,
    parameter [288-1:0] INIT_RAM_1C = 0,
    parameter [288-1:0] INIT_RAM_1D = 0,
    parameter [288-1:0] INIT_RAM_1E = 0,
    parameter [288-1:0] INIT_RAM_1F = 0,
    parameter [288-1:0] INIT_RAM_20 = 0,
    parameter [288-1:0] INIT_RAM_21 = 0,
    parameter [288-1:0] INIT_RAM_22 = 0,
    parameter [288-1:0] INIT_RAM_23 = 0,
    parameter [288-1:0] INIT_RAM_24 = 0,
    parameter [288-1:0] INIT_RAM_25 = 0,
    parameter [288-1:0] INIT_RAM_26 = 0,
    parameter [288-1:0] INIT_RAM_27 = 0,
    parameter [288-1:0] INIT_RAM_28 = 0,
    parameter [288-1:0] INIT_RAM_29 = 0,
    parameter [288-1:0] INIT_RAM_2A = 0,
    parameter [288-1:0] INIT_RAM_2B = 0,
    parameter [288-1:0] INIT_RAM_2C = 0,
    parameter [288-1:0] INIT_RAM_2D = 0,
    parameter [288-1:0] INIT_RAM_2E = 0,
    parameter [288-1:0] INIT_RAM_2F = 0,
    parameter [288-1:0] INIT_RAM_30 = 0,
    parameter [288-1:0] INIT_RAM_31 = 0,
    parameter [288-1:0] INIT_RAM_32 = 0,
    parameter [288-1:0] INIT_RAM_33 = 0,
    parameter [288-1:0] INIT_RAM_34 = 0,
    parameter [288-1:0] INIT_RAM_35 = 0,
    parameter [288-1:0] INIT_RAM_36 = 0,
    parameter [288-1:0] INIT_RAM_37 = 0,
    parameter [288-1:0] INIT_RAM_38 = 0,
    parameter [288-1:0] INIT_RAM_39 = 0,
    parameter [288-1:0] INIT_RAM_3A = 0,
    parameter [288-1:0] INIT_RAM_3B = 0,
    parameter [288-1:0] INIT_RAM_3C = 0,
    parameter [288-1:0] INIT_RAM_3D = 0,
    parameter [288-1:0] INIT_RAM_3E = 0,
    parameter [288-1:0] INIT_RAM_3F = 0,
    parameter UNUSED = 0
) (
    input  wire          CLKA, CEA, OCEA, RESETA, WREA,
    input  wire          CLKB, CEB, OCEB, RESETB, WREB,
    input  wire [13:0]   ADA, ADB,
    input  wire [2:0]    BLKSELA, BLKSELB,
    input  wire [17:0]   DIA, DIB,
    output wire [17:0]   DOA, DOB
);
    localparam SIZE = 18432;
    reg mem [0:SIZE-1];
    reg [17:0] ra, rb, pa, pb;
    integer i;
    initial begin
        for (i = 0; i < SIZE; i = i + 1) mem[i] = 1'b0;
        ra = 0; rb = 0; pa = 0; pb = 0;
    end

    function integer shift_of(input integer w);
        integer shift;
        begin
            shift = 0;
            case (w) 9: shift = 3; 18: shift = 4; default: shift = 0; endcase
            shift_of = shift;
        end
    endfunction

    task automatic port(input integer w, input integer wmode, input [13:0] ad, input we,
                        input [17:0] di, inout [17:0] r);
        integer base, k;
        reg [17:0] old;
        begin
            base = (ad >> shift_of(w)) * w;
            old = 0;
            for (k = 0; k < w; k = k + 1) old[k] = mem[(base + k) % SIZE];
            if (we) begin
                for (k = 0; k < w; k = k + 1)
                    if (w < 16 || ad[k / (w / 2)])      // 16/18-bit: AD[1:0] byte enables
                        mem[(base + k) % SIZE] = di[k];
                if (wmode == 1) r = di;
                else if (wmode == 2) r = old;
            end else begin
                r = old;
            end
        end
    endtask

    always @(posedge CLKA) begin
        if (RESETA) begin ra <= 0; pa <= 0; end
        else begin
            if (CEA && BLKSELA == BLK_SEL_0) port(BIT_WIDTH_0, WRITE_MODE0, ADA, WREA, DIA, ra);
            if (OCEA) pa <= ra;
        end
    end
    always @(posedge CLKB) begin
        if (RESETB) begin rb <= 0; pb <= 0; end
        else begin
            if (CEB && BLKSELB == BLK_SEL_1) port(BIT_WIDTH_1, WRITE_MODE1, ADB, WREB, DIB, rb);
            if (OCEB) pb <= rb;
        end
    end
    assign DOA = READ_MODE0 ? pa : ra;
    assign DOB = READ_MODE1 ? pb : rb;
endmodule
