// SPI mode 0 byte shifter (SCK idle low, MSB first).
// Each SCK half period lasts (div + 1) clock cycles.
// Input is sampled on the rising SCK edge, or just before the falling edge
// when `sample_late` is set (helps with long wires / slow chips).
//
// quad = 0: one bit per clock, MOSI out / MISO in (8 SCK periods per byte).
// quad = 1: quad input, four bits per clock on IO3..IO0 (2 SCK periods per
//           byte, used for the data phase of 6Bh "fast read quad output").
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
    input  wire       quad,
    input  wire       req,
    input  wire [7:0] tx,
    output reg        ack,
    output reg  [7:0] rx,
    output reg        rvalid,
    output reg        busy,
    output reg        sck,
    output wire       mosi,
    input  wire [3:0] qin        // {IO3, IO2, IO1 (MISO), IO0}
);
    reg [7:0] sh_out;
    reg [7:0] sh_in;
    reg [2:0] bitn;
    reg [7:0] cnt;
    reg       phase;   // 0 = SCK low half, 1 = SCK high half
    reg       q;       // quad mode latched for the current byte

    assign mosi = sh_out[7];

    wire [7:0] shifted_in = q ? {sh_in[3:0], qin} : {sh_in[6:0], qin[1]};
    wire       take_idle  = !busy && req && !ack;

    always @(posedge clk) begin
        ack    <= 1'b0;
        rvalid <= 1'b0;
        if (rst) begin
            busy   <= 1'b0;
            sck    <= 1'b0;
            sh_out <= 8'h00;
            q      <= 1'b0;
        end else if (!busy) begin
            if (take_idle) begin
                ack    <= 1'b1;
                busy   <= 1'b1;
                sh_out <= tx;
                q      <= quad;
                bitn   <= quad ? 3'd1 : 3'd7;
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
                        q      <= quad;
                        bitn   <= quad ? 3'd1 : 3'd7;
                    end else begin
                        busy <= 1'b0;
                    end
                end else begin
                    bitn   <= bitn - 1'b1;
                    sh_out <= q ? sh_out : {sh_out[6:0], 1'b0};
                end
            end
        end
    end
endmodule
