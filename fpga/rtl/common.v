// Small shared building blocks.
//
// Convention used across this design: every register that drives an
// active-low pin is stored as an active-high "_act" bit, so the all-zero
// power-up state of the FPGA keeps every pin inactive (CE#/WE#/RE#/CS# high).

// Two-flop synchronizer for asynchronous inputs.
module sync2 #(
    parameter W = 1
) (
    input  wire         clk,
    input  wire [W-1:0] d,
    output wire [W-1:0] q
);
    reg [W-1:0] s1, s2;
    always @(posedge clk) begin
        s1 <= d;
        s2 <= s1;
    end
    assign q = s2;
endmodule

// First-word-fall-through byte FIFO backed by block RAM.
// `dout`/`valid` present the head byte; pulse `rd` (only while `valid`) to
// consume it. `level` counts all stored bytes including the head register.
module fifo8 #(
    parameter AW = 12
) (
    input  wire        clk,
    input  wire        rst,
    input  wire        wr,
    input  wire [7:0]  din,
    output wire        full,
    input  wire        rd,
    output reg  [7:0]  dout,
    output reg         valid,
    output wire [AW:0] level
);
    reg [7:0]  mem [0:(1<<AW)-1];
    reg [AW:0] wp, rp;

    // Occupancy must be computed at pointer width: in a 32-bit context the
    // subtraction goes negative after the pointers wrap and "full" is missed.
    wire [AW:0] used = wp - rp;
    wire mem_empty = (wp == rp);
    assign full    = (used == {1'b1, {AW{1'b0}}});
    wire do_wr     = wr && !full;
    wire fetch     = !mem_empty && (!valid || rd);

    always @(posedge clk) begin
        if (do_wr)
            mem[wp[AW-1:0]] <= din;
        if (fetch)
            dout <= mem[rp[AW-1:0]];
    end

    always @(posedge clk) begin
        if (rst) begin
            wp    <= 0;
            rp    <= 0;
            valid <= 1'b0;
        end else begin
            if (do_wr)
                wp <= wp + 1'b1;
            if (fetch) begin
                rp    <= rp + 1'b1;
                valid <= 1'b1;
            end else if (rd) begin
                valid <= 1'b0;
            end
        end
    end

    assign level = used + {{AW{1'b0}}, valid};
endmodule

// Stretches single-cycle activity pulses so they are visible on an LED.
module pulse_stretch #(
    parameter W = 21
) (
    input  wire clk,
    input  wire in,
    output wire out
);
    reg [W-1:0] cnt;
    always @(posedge clk) begin
        if (in)
            cnt <= {W{1'b1}};
        else if (cnt != 0)
            cnt <= cnt - 1'b1;
    end
    assign out = (cnt != 0);
endmodule

// Dual-clock byte FIFO (Gray-coded pointers, block RAM), first-word-fall-
// through on the read side. `wroom` = at least ROOM free entries (seen from
// the write clock).
module afifo8 #(
    parameter AW   = 9,
    parameter ROOM = 8
) (
    input  wire        wclk,
    input  wire        wrst,
    input  wire        wr,
    input  wire [7:0]  din,
    output wire        wroom,
    input  wire        rclk,
    input  wire        rrst,
    input  wire        rd,
    output reg  [7:0]  dout,
    output reg         valid,
    output wire        more         // another entry behind the head
);
    reg [7:0]  mem [0:(1<<AW)-1];

    function [AW:0] bin2gray(input [AW:0] b);
        bin2gray = b ^ (b >> 1);
    endfunction
    function [AW:0] gray2bin(input [AW:0] g);
        integer i;
        begin
            gray2bin[AW] = g[AW];
            for (i = AW - 1; i >= 0; i = i - 1)
                gray2bin[i] = gray2bin[i + 1] ^ g[i];
        end
    endfunction

    // ---- write side
    reg  [AW:0] wp, wp_g;
    reg  [AW:0] rp_g_w1, rp_g_w2;
    wire [AW:0] rp_w   = gray2bin(rp_g_w2);
    wire [AW:0] wused  = wp - rp_w;
    assign wroom = (wused <= (1 << AW) - ROOM);
    wire do_wr = wr && (wused != (1 << AW));

    always @(posedge wclk) begin
        if (do_wr)
            mem[wp[AW-1:0]] <= din;
    end
    always @(posedge wclk) begin
        if (wrst) begin
            wp      <= 0;
            wp_g    <= 0;
            rp_g_w1 <= 0;
            rp_g_w2 <= 0;
        end else begin
            rp_g_w1 <= rp_g;
            rp_g_w2 <= rp_g_w1;
            if (do_wr) begin
                wp   <= wp + 1'b1;
                wp_g <= bin2gray(wp + 1'b1);
            end
        end
    end

    // ---- read side
    reg  [AW:0] rp, rp_g;
    reg  [AW:0] wp_g_r1, wp_g_r2;
    wire mem_empty = (rp_g == wp_g_r2);
    wire fetch     = !mem_empty && (!valid || rd);
    assign more    = !mem_empty;

    always @(posedge rclk) begin
        if (fetch)
            dout <= mem[rp[AW-1:0]];
    end
    always @(posedge rclk) begin
        if (rrst) begin
            rp      <= 0;
            rp_g    <= 0;
            wp_g_r1 <= 0;
            wp_g_r2 <= 0;
            valid   <= 1'b0;
        end else begin
            wp_g_r1 <= wp_g;
            wp_g_r2 <= wp_g_r1;
            if (fetch) begin
                rp    <= rp + 1'b1;
                rp_g  <= bin2gray(rp + 1'b1);
                valid <= 1'b1;
            end else if (rd) begin
                valid <= 1'b0;
            end
        end
    end
endmodule
