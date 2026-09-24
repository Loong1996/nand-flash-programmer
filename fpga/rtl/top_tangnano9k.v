// Top level for Sipeed Tang Nano 9K (GW1NR-9, 27 MHz crystal).
//
// Host links: on-board BL702 USB-UART (always available) and an optional
// FT232H in 245 asynchronous FIFO mode. The engine answers on whichever link
// delivered the most recent command byte.
//
// The FT232H wiring is already complete for 245 *synchronous* FIFO mode
// (CLKOUT on global clock pin 35, OE#, SIWU#); this gateware only watches
// those three pins, so a later sync-FIFO build needs no rewiring.

module top #(
    parameter CLK_HZ = 27_000_000,
    parameter BAUD   = 115_200
) (
    input  wire       clk27,
    input  wire [1:0] btn_n,
    output wire [5:0] led_n,

    output wire       uart_tx,
    input  wire       uart_rx,

    inout  wire [7:0] nand_io,
    output wire       nand_cle,
    output wire       nand_ale,
    output wire       nand_we_n,
    output wire       nand_re_n,
    output wire       nand_ce_n,
    output wire       nand_wp_n,
    input  wire       nand_rb_n,

    output wire       spi_cs_n,
    output wire       spi_sck,
    output wire       spi_io0,     // MOSI / DI
    input  wire       spi_io1,     // MISO / DO
    output wire       spi_io2,     // WP#
    output wire       spi_io3,     // HOLD# / RESET#

    inout  wire [7:0] ft_d,
    input  wire       ft_rxf_n,
    input  wire       ft_txe_n,
    output wire       ft_rd_n,
    output wire       ft_wr_n,
    input  wire       ft_clkout,   // AC5, reserved for sync FIFO (input only here)
    input  wire       ft_oe_n,     // AC6, reserved for sync FIFO (input only here)
    input  wire       ft_siwu_n    // AC4, reserved (input only here)
);
    localparam [15:0] BAUD_DIV = (CLK_HZ + BAUD / 2) / BAUD;

    wire clk = clk27;

    // ------------------------------------------------------------ reset
    reg  [7:0] por = 8'd0;
    wire [1:0] btn_s;
    sync2 #(.W(2)) u_btn (.clk(clk), .d(btn_n), .q(btn_s));
    always @(posedge clk)
        if (!por[7])
            por <= por + 1'b1;
    wire rst = !por[7] || !btn_s[0];

    // ------------------------------------------------------------ inputs
    wire uart_rx_s, rxf_n_s, txe_n_s, rb_s;
    sync2 #(.W(4)) u_sync (
        .clk(clk),
        .d({uart_rx, ft_rxf_n, ft_txe_n, nand_rb_n}),
        .q({uart_rx_s, rxf_n_s, txe_n_s, rb_s})
    );

    // FT232H sync-FIFO pins: diagnostics only. CLKOUT counts as active when
    // it toggled within the last ~2.4 ms (FT232H in sync FIFO mode).
    wire [2:0] ftx_s;
    sync2 #(.W(3)) u_ftx (.clk(clk), .d({ft_clkout, ft_siwu_n, ft_oe_n}), .q(ftx_s));
    reg        clk_prev;
    reg [15:0] clk_idle;
    always @(posedge clk) begin
        clk_prev <= ftx_s[2];
        if (rst)
            clk_idle <= 16'hFFFF;
        else if (ftx_s[2] != clk_prev)
            clk_idle <= 16'd0;
        else if (clk_idle != 16'hFFFF)
            clk_idle <= clk_idle + 1'b1;
    end
    wire [2:0] ft_status = {clk_idle != 16'hFFFF, ftx_s[1], ftx_s[0]};

    // ------------------------------------------------------------ FIFOs
    localparam RX_AW = 12, TX_AW = 12;

    wire              rxf_wr;
    wire [7:0]        rxf_din;
    wire              rxf_full;
    wire              rxf_rd;
    wire [7:0]        rxf_dout;
    wire              rxf_valid;
    wire [RX_AW:0]    rxf_level;

    fifo8 #(.AW(RX_AW)) u_rxf (
        .clk(clk), .rst(rst), .wr(rxf_wr), .din(rxf_din), .full(rxf_full),
        .rd(rxf_rd), .dout(rxf_dout), .valid(rxf_valid), .level(rxf_level)
    );

    wire              txf_wr;
    wire [7:0]        txf_din;
    wire              txf_full;
    wire              txf_rd;
    wire [7:0]        txf_dout;
    wire              txf_valid;
    wire [TX_AW:0]    txf_level;

    fifo8 #(.AW(TX_AW)) u_txf (
        .clk(clk), .rst(rst), .wr(txf_wr), .din(txf_din), .full(txf_full),
        .rd(txf_rd), .dout(txf_dout), .valid(txf_valid), .level(txf_level)
    );

    // ------------------------------------------------------------ UART
    wire [15:0] baud_div;
    wire        urx_valid;
    wire [7:0]  urx_data;
    wire        utx_busy;
    reg         utx_start;

    uart_rx u_urx (.clk(clk), .rst(rst), .rxd(uart_rx_s), .div(baud_div),
                   .valid(urx_valid), .data(urx_data));
    uart_tx u_utx (.clk(clk), .rst(rst), .div(baud_div), .start(utx_start),
                   .data(txf_dout), .busy(utx_busy), .txd(uart_tx));

    // ------------------------------------------------------------ FT245
    wire [7:0] ft_d_out;
    wire       ft_d_oe;
    wire       frx_valid;
    wire [7:0] frx_data;
    wire       ftx_pop;
    reg        active_port;   // 0 = UART, 1 = FT245

    assign ft_d = ft_d_oe ? ft_d_out : 8'bz;

    ft245_async u_ft (
        .clk(clk), .rst(rst), .rxf_n_s(rxf_n_s), .txe_n_s(txe_n_s),
        .d_in(ft_d), .d_out(ft_d_out), .d_oe(ft_d_oe), .rd_n(ft_rd_n), .wr_n(ft_wr_n),
        .rx_space(rxf_level < ((1 << RX_AW) - 8)),
        .rx_valid(frx_valid), .rx_data(frx_data),
        .tx_avail(active_port && txf_valid), .tx_data(txf_dout), .tx_pop(ftx_pop)
    );

    // ------------------------------------------------------------ port mux
    always @(posedge clk) begin
        if (rst)
            active_port <= 1'b0;
        else if (frx_valid)
            active_port <= 1'b1;
        else if (urx_valid)
            active_port <= 1'b0;
    end

    assign rxf_wr  = urx_valid | frx_valid;
    assign rxf_din = frx_valid ? frx_data : urx_data;
    wire rx_overflow = urx_valid && (rxf_full || frx_valid);

    reg utx_pop;
    always @(posedge clk) begin
        utx_start <= 1'b0;
        utx_pop   <= 1'b0;
        if (!rst && !active_port && txf_valid && !utx_busy && !utx_start && !utx_pop) begin
            utx_start <= 1'b1;
            utx_pop   <= 1'b1;
        end
    end
    assign txf_rd = utx_pop | ftx_pop;

    wire tx_drained = !txf_valid && (txf_level == 0) && !utx_busy && !utx_start;

    // ------------------------------------------------------------ engine
    wire       e_cle, e_ale, e_we, e_re, e_ce, e_wp_hi, e_io_oe, e_nand_park;
    wire [7:0] e_io_o;
    wire       e_cs, e_sck, e_mosi, e_io2_hi, e_io3_hi, e_spi_park;
    wire       nand_act, spi_act, err;

    engine #(.CLK_HZ(CLK_HZ), .BAUD_DIV_DEFAULT(BAUD_DIV), .RX_AW(RX_AW)) u_eng (
        .clk(clk), .rst(rst),
        .rx_valid(rxf_valid), .rx_data(rxf_dout), .rx_pop(rxf_rd),
        .tx_full(txf_full), .tx_push(txf_wr), .tx_data(txf_din), .tx_drained(tx_drained),
        .nand_cle(e_cle), .nand_ale(e_ale), .nand_we_act(e_we), .nand_re_act(e_re),
        .nand_ce_act(e_ce), .nand_wp_hi(e_wp_hi), .nand_io_o(e_io_o), .nand_io_oe(e_io_oe),
        .nand_io_i(nand_io), .nand_rb(rb_s), .nand_park(e_nand_park),
        .spi_cs_act(e_cs), .spi_sck(e_sck), .spi_mosi(e_mosi), .spi_miso(spi_io1),
        .spi_io2_hi(e_io2_hi), .spi_io3_hi(e_io3_hi), .spi_park(e_spi_park),
        .baud_div(baud_div), .active_port(active_port), .ft_status(ft_status),
        .rx_overflow(rx_overflow),
        .nand_activity(nand_act), .spi_activity(spi_act), .err_led(err)
    );

    // ------------------------------------------------------------ pins
    assign nand_io   = (e_io_oe && !e_nand_park) ? e_io_o : 8'bz;
    assign nand_cle  = e_nand_park ? 1'bz : e_cle;
    assign nand_ale  = e_nand_park ? 1'bz : e_ale;
    assign nand_we_n = e_nand_park ? 1'bz : ~e_we;
    assign nand_re_n = e_nand_park ? 1'bz : ~e_re;
    assign nand_ce_n = e_nand_park ? 1'bz : ~e_ce;
    assign nand_wp_n = e_nand_park ? 1'bz : e_wp_hi;

    assign spi_cs_n  = e_spi_park ? 1'bz : ~e_cs;
    assign spi_sck   = e_spi_park ? 1'bz : e_sck;
    assign spi_io0   = e_spi_park ? 1'bz : e_mosi;
    assign spi_io2   = e_spi_park ? 1'bz : e_io2_hi;
    assign spi_io3   = e_spi_park ? 1'bz : e_io3_hi;

    // ------------------------------------------------------------ LEDs (active low)
    reg [23:0] hb;
    always @(posedge clk) hb <= hb + 1'b1;

    wire led_uart, led_ft, led_nand, led_spi;
    pulse_stretch u_ls0 (.clk(clk), .in(urx_valid | utx_start), .out(led_uart));
    pulse_stretch u_ls1 (.clk(clk), .in(frx_valid | ftx_pop),   .out(led_ft));
    pulse_stretch u_ls2 (.clk(clk), .in(nand_act),              .out(led_nand));
    pulse_stretch u_ls3 (.clk(clk), .in(spi_act),               .out(led_spi));

    assign led_n = ~{err, led_spi, led_nand, led_ft, led_uart, hb[23]};
endmodule
