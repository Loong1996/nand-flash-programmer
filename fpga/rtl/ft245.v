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

// FTDI FT232H "245 synchronous FIFO" bridge, clocked by the FT232H CLKOUT
// (60 MHz). All FT232H outputs are sampled on the rising edge and every
// signal going to the FT232H comes straight from a register.
//
// Read  (host -> FPGA): with RXF# low, pull OE# low (FT232H drives D), one
//                       clock later pull RD# low. Every rising edge that sees
//                       RD# and RXF# low transfers the byte on D.
// Write (FPGA -> host): drive D and WR# low; every rising edge that sees WR#
//                       and TXE# low transfers the byte. A byte presented while
//                       TXE# is high stays at the FIFO head until it is taken.
// Bursts are limited to MAX_BURST bytes while the other direction has work,
// and a write stalled by TXE# gives way to pending reads, so the host can
// never deadlock the bridge by writing and reading at the same time.

module ft245_sync #(
    parameter MAX_BURST = 1024
) (
    input  wire       clk,         // FT232H CLKOUT
    input  wire       rst,
    input  wire       rxf_n,
    input  wire       txe_n,
    input  wire [7:0] d_in,
    output wire [7:0] d_out,
    output reg        d_oe,
    output reg        oe_n,
    output reg        rd_n,
    output reg        wr_n,
    // host -> FPGA (write port of a FIFO)
    input  wire       rx_room,
    output reg        rx_wr,
    output reg  [7:0] rx_data,
    // FPGA -> host (FWFT read port of a FIFO)
    input  wire       tx_valid,
    input  wire       tx_more,     // another byte behind the head
    input  wire [7:0] tx_data,
    output wire       tx_rd
);
    localparam IDLE = 2'd0, RD_OE = 2'd1, RD = 2'd2, WR = 2'd3;

    reg [1:0]  st;
    reg [10:0] cnt;

    assign d_out = tx_data;

    // The byte on the bus is taken by the FT232H at this edge.
    wire wr_taken = (st == WR) && !wr_n && !txe_n;
    assign tx_rd  = wr_taken;
    wire burst_up = (cnt >= MAX_BURST - 1);

    always @(posedge clk) begin
        rx_wr <= 1'b0;
        if (rst) begin
            st   <= IDLE;
            oe_n <= 1'b1;
            rd_n <= 1'b1;
            wr_n <= 1'b1;
            d_oe <= 1'b0;
            cnt  <= 0;
        end else begin
            case (st)
                IDLE: begin
                    cnt <= 0;
                    if (!rxf_n && rx_room) begin
                        oe_n <= 1'b0;
                        st   <= RD_OE;
                    end else if (tx_valid && !txe_n) begin
                        d_oe <= 1'b1;
                        wr_n <= 1'b0;
                        st   <= WR;
                    end
                end
                RD_OE: begin
                    rd_n <= 1'b0;
                    st   <= RD;
                end
                RD: begin
                    if (!rxf_n) begin
                        rx_wr   <= 1'b1;
                        rx_data <= d_in;
                        cnt     <= cnt + 1'b1;
                    end
                    if (rxf_n || !rx_room || (tx_valid && !txe_n && burst_up)) begin
                        rd_n <= 1'b1;
                        oe_n <= 1'b1;
                        st   <= IDLE;
                    end
                end
                default: begin // WR
                    if (wr_taken)
                        cnt <= cnt + 1'b1;
                    if ((wr_taken && !tx_more) ||                 // FIFO runs dry
                        (!wr_taken && !tx_valid) ||
                        (!rxf_n && rx_room && (txe_n || burst_up))) begin
                        wr_n <= 1'b1;
                        d_oe <= 1'b0;
                        st   <= IDLE;
                    end
                end
            endcase
        end
    end
endmodule
