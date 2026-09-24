// SPI mode 0 byte shifter (SCK idle low, MSB first).
// Each SCK half period lasts (div + 1) clock cycles.
// Input is sampled on the rising SCK edge, or just before the falling edge
// when `sample_late` is set (helps with long wires / slow chips).
//
// width = 0: one bit per clock, MOSI out / MISO in (8 SCK periods per byte).
// width = 1: two bits per clock on IO1..IO0 (dual; 4 SCK periods per byte).
// width = 2: four bits per clock on IO3..IO0 (quad; 2 SCK periods per byte).
// Whether IO lines are driven (qout) or sampled (qin) is decided by the
// caller; the shifter always shifts both directions. The width is latched
// per byte.
//
// Handshake as in nand_bus: hold `req` with `tx` until the one-cycle `ack`.
// A request is taken when idle or on the last SCK falling edge of the current
// byte, so a client that keeps `req` asserted gets a continuous clock. The
// received byte is reported with a one-cycle `rvalid` strobe.

module spi_master (
    input  wire       clk,
    input  wire       rst,
    input  wire [7:0] div,
    input  wire       sample_late,
    input  wire [1:0] width,
    input  wire       req,
    input  wire [7:0] tx,
    output reg        ack,
    output reg  [7:0] rx,
    output reg        rvalid,
    output reg        busy,
    output reg        sck,
    output wire       mosi,
    output wire [3:0] qout,      // {IO3, IO2, IO1, IO0} data for wide writes (IO0 = MOSI in x1)
    input  wire [3:0] qin        // {IO3, IO2, IO1 (MISO), IO0}
);
    reg [7:0] sh_out;
    reg [7:0] sh_in;
    reg [2:0] bitn;
    reg [7:0] cnt;
    reg       phase;   // 0 = SCK low half, 1 = SCK high half
    reg [1:0] w;       // width latched for the current byte

    assign mosi = sh_out[7];
    assign qout = (w == 2'd2) ? sh_out[7:4] :
                  (w == 2'd1) ? {2'b11, sh_out[7:6]} : {3'b111, sh_out[7]};

    wire [7:0] shifted_in = (w == 2'd2) ? {sh_in[3:0], qin} :
                            (w == 2'd1) ? {sh_in[5:0], qin[1:0]} : {sh_in[6:0], qin[1]};
    wire [7:0] shifted_out = (w == 2'd2) ? {sh_out[3:0], 4'h0} :
                             (w == 2'd1) ? {sh_out[5:0], 2'b00} : {sh_out[6:0], 1'b0};
    wire [2:0] last_bit = (width == 2'd2) ? 3'd1 : (width == 2'd1) ? 3'd3 : 3'd7;
    wire       take_idle  = !busy && req && !ack;

    always @(posedge clk) begin
        ack    <= 1'b0;
        rvalid <= 1'b0;
        if (rst) begin
            busy   <= 1'b0;
            sck    <= 1'b0;
            sh_out <= 8'h00;
            w      <= 2'd0;
        end else if (!busy) begin
            if (take_idle) begin
                ack    <= 1'b1;
                busy   <= 1'b1;
                sh_out <= tx;
                w      <= width;
                bitn   <= last_bit;
                cnt    <= div;
                phase  <= 1'b0;
                sck    <= 1'b0;
            end
        end else if (cnt != 0) begin
            cnt <= cnt - 1'b1;
        end else begin
            cnt <= div;
            if (!phase) begin
                sck   <= 1'b1;
                phase <= 1'b1;
                if (!sample_late)
                    sh_in <= shifted_in;
            end else begin
                sck   <= 1'b0;
                phase <= 1'b0;
                if (sample_late)
                    sh_in <= shifted_in;
                if (bitn == 3'd0) begin
                    rvalid <= 1'b1;
                    rx     <= sample_late ? shifted_in : sh_in;
                    if (req && !ack) begin
                        // next byte follows without a gap
                        ack    <= 1'b1;
                        sh_out <= tx;
                        w      <= width;
                        bitn   <= last_bit;
                    end else begin
                        busy <= 1'b0;
                    end
                end else begin
                    bitn   <= bitn - 1'b1;
                    sh_out <= shifted_out;
                end
            end
        end
    end
endmodule
