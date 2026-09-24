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
