// SPI mode 0 byte shifter (SCK idle low, MSB first).
// Each SCK half period lasts (div + 1) clock cycles.
// MISO is sampled on the rising SCK edge, or just before the falling edge
// when `sample_late` is set (helps with long wires / slow chips).

module spi_master (
    input  wire       clk,
    input  wire       rst,
    input  wire [7:0] div,
    input  wire       sample_late,
    input  wire       start,
    input  wire [7:0] tx,
    output reg  [7:0] rx,
    output reg        busy,
    output reg        done,
    output reg        sck,
    output wire       mosi,
    input  wire       miso
);
    reg [7:0] sh_out;
    reg [7:0] sh_in;
    reg [2:0] bitn;
    reg [7:0] cnt;
    reg       phase;   // 0 = SCK low half, 1 = SCK high half

    assign mosi = sh_out[7];

    wire [7:0] shifted_in = {sh_in[6:0], miso};

    always @(posedge clk) begin
        done <= 1'b0;
        if (rst) begin
            busy   <= 1'b0;
            sck    <= 1'b0;
            sh_out <= 8'h00;
        end else if (!busy) begin
            if (start) begin
                busy   <= 1'b1;
                sh_out <= tx;
                bitn   <= 3'd7;
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
                    busy <= 1'b0;
                    done <= 1'b1;
                    rx   <= sample_late ? shifted_in : sh_in;
                end else begin
                    bitn   <= bitn - 1'b1;
                    sh_out <= {sh_out[6:0], 1'b0};
                end
            end
        end
    end
endmodule
