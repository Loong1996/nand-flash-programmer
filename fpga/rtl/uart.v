// 8N1 UART with a run-time divisor (clock cycles per bit).

module uart_rx (
    input  wire        clk,
    input  wire        rst,
    input  wire        rxd,      // already synchronized
    input  wire [15:0] div,
    output reg         valid,
    output reg  [7:0]  data
);
    reg        busy;
    reg [15:0] cnt;
    reg [3:0]  bitn;
    reg [7:0]  sh;

    always @(posedge clk) begin
        valid <= 1'b0;
        if (rst) begin
            busy <= 1'b0;
        end else if (!busy) begin
            if (!rxd) begin
                busy <= 1'b1;
                cnt  <= {1'b0, div[15:1]};   // move to the middle of the start bit
                bitn <= 4'd0;
            end
        end else if (cnt != 0) begin
            cnt <= cnt - 1'b1;
        end else begin
            cnt <= div - 1'b1;
            if (bitn == 4'd0) begin
                if (rxd)
                    busy <= 1'b0;            // glitch, not a start bit
                bitn <= 4'd1;
            end else if (bitn <= 4'd8) begin
                sh   <= {rxd, sh[7:1]};
                bitn <= bitn + 1'b1;
            end else begin
                busy <= 1'b0;
                if (rxd) begin               // valid stop bit
                    valid <= 1'b1;
                    data  <= sh;
                end
            end
        end
    end
endmodule

module uart_tx (
    input  wire        clk,
    input  wire        rst,
    input  wire [15:0] div,
    input  wire        start,
    input  wire [7:0]  data,
    output wire        busy,
    output wire        txd
);
    reg        act;
    reg        low;      // active-high "drive line low"; idle (0) = line high
    reg [9:0]  sh;
    reg [3:0]  n;
    reg [15:0] cnt;

    assign busy = act;
    assign txd  = ~low;

    always @(posedge clk) begin
        if (rst) begin
            act <= 1'b0;
            low <= 1'b0;
        end else if (!act) begin
            if (start) begin
                sh  <= {1'b1, data, 1'b0};
                act <= 1'b1;
                n   <= 4'd0;
                cnt <= 16'd0;
            end
        end else if (cnt != 0) begin
            cnt <= cnt - 1'b1;
        end else if (n == 4'd10) begin
            act <= 1'b0;
            low <= 1'b0;
        end else begin
            low <= ~sh[0];
            sh  <= {1'b1, sh[9:1]};
            n   <= n + 1'b1;
            cnt <= div - 1'b1;
        end
    end
endmodule
