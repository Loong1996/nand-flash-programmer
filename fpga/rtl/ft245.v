// FTDI FT232H "245 asynchronous FIFO" bridge.
//
// Read  (host -> FPGA): RXF# low means a byte is waiting. Pull RD# low,
//                       sample D after T_STROBE cycles, release RD#.
// Write (FPGA -> host): TXE# low means there is room. Drive D, pulse WR#
//                       low for T_STROBE cycles, keep D one more cycle.
// After every access wait T_RECOVER cycles so that the synchronized
// RXF#/TXE# flags reflect the new FT232H state before the next decision.
// Reads and writes alternate when both are pending.

module ft245_async #(
    parameter T_STROBE  = 3,
    parameter T_RECOVER = 5
) (
    input  wire       clk,
    input  wire       rst,
    input  wire       rxf_n_s,     // synchronized RXF#
    input  wire       txe_n_s,     // synchronized TXE#
    input  wire [7:0] d_in,
    output reg  [7:0] d_out,
    output reg        d_oe,
    output wire       rd_n,
    output wire       wr_n,
    // host -> FPGA stream
    input  wire       rx_space,
    output reg        rx_valid,
    output reg  [7:0] rx_data,
    // FPGA -> host stream (FWFT head)
    input  wire       tx_avail,
    input  wire [7:0] tx_data,
    output reg        tx_pop
);
    localparam IDLE = 3'd0, RD = 3'd1, WSU = 3'd2, WR = 3'd3, WH = 3'd4, REC = 3'd5;

    reg       rd_act, wr_act;
    reg [2:0] st;
    reg [3:0] cnt;
    reg       prefer_tx;

    assign rd_n = ~rd_act;
    assign wr_n = ~wr_act;

    wire can_rd = !rxf_n_s && rx_space;
    wire can_wr = !txe_n_s && tx_avail;

    always @(posedge clk) begin
        rx_valid <= 1'b0;
        tx_pop   <= 1'b0;
        if (rst) begin
            st        <= IDLE;
            rd_act    <= 1'b0;
            wr_act    <= 1'b0;
            d_oe      <= 1'b0;
            prefer_tx <= 1'b0;
        end else begin
            case (st)
                IDLE: begin
                    if (can_rd && !(prefer_tx && can_wr)) begin
                        rd_act    <= 1'b1;
                        cnt       <= T_STROBE - 1;
                        prefer_tx <= 1'b1;
                        st        <= RD;
                    end else if (can_wr) begin
                        d_out     <= tx_data;
                        d_oe      <= 1'b1;
                        prefer_tx <= 1'b0;
                        st        <= WSU;
                    end
                end
                RD: begin
                    if (cnt == 0) begin
                        rx_data  <= d_in;
                        rx_valid <= 1'b1;
                        rd_act   <= 1'b0;
                        cnt      <= T_RECOVER - 1;
                        st       <= REC;
                    end else begin
                        cnt <= cnt - 1'b1;
                    end
                end
                WSU: begin
                    wr_act <= 1'b1;
                    cnt    <= T_STROBE - 1;
                    st     <= WR;
                end
                WR: begin
                    if (cnt == 0) begin
                        wr_act <= 1'b0;
                        tx_pop <= 1'b1;
                        st     <= WH;
                    end else begin
                        cnt <= cnt - 1'b1;
                    end
                end
                WH: begin
                    d_oe <= 1'b0;
                    cnt  <= T_RECOVER - 1;
                    st   <= REC;
                end
                default: begin // REC
                    if (cnt == 0)
                        st <= IDLE;
                    else
                        cnt <= cnt - 1'b1;
                end
            endcase
        end
    end
endmodule
