// Top level for Sipeed Tang Nano 9K (GW1NR-9, 27 MHz crystal).
//
// Host links: on-board BL702 USB-UART (always available) and an optional
// FT232H. The engine answers on whichever link delivered the most recent
// command byte.
//
// The FT232H runs in 245 asynchronous FIFO mode, or in 245 *synchronous* FIFO
// mode when the host selects it: the FT232H then outputs a 60 MHz CLKOUT (on
// global clock pin 36) and this design switches to the ft245_sync bridge in
// that clock domain, driving OE# itself. Without CLKOUT, OE# is left floating.

module top #(
    parameter CLK_HZ = 27_000_000,
    parameter BAUD   = 115_200
) (
    input  wire       clk27,
    input  wire [1:0] btn_n,
    output wire [5:0] led_n,

    output wire       uart_tx,
    input  wire       uart_rx,

    // All flash-side pins are bidirectional so the pin test (PIN_TEST op) can
    // drive any single line and read back every pad.
    inout  wire [7:0] nand_io,
    inout  wire       nand_cle,
    inout  wire       nand_ale,
    inout  wire       nand_we_n,
    inout  wire       nand_re_n,
    inout  wire       nand_ce_n,
    inout  wire       nand_wp_n,
    inout  wire       nand_rb_n,

    inout  wire       spi_cs_n,
    inout  wire       spi_sck,
    inout  wire       spi_io0,     // MOSI / DI  (IO0 in quad mode)
    inout  wire       spi_io1,     // MISO / DO  (IO1)
    inout  wire       spi_io2,     // WP#        (IO2)
    inout  wire       spi_io3,     // HOLD#      (IO3)

    inout  wire [7:0] ft_d,
    input  wire       ft_rxf_n,
    input  wire       ft_txe_n,
    output wire       ft_rd_n,
    output wire       ft_wr_n,
    input  wire       ft_clkout,   // AC5, 60 MHz in sync FIFO mode
    inout  wire       ft_oe_n,     // AC6, driven only in sync FIFO mode
    input  wire       ft_siwu_n    // AC4, not used (kept high by a pull-up)
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

    // CLKOUT counts as active when it toggled within the last ~2.4 ms
    // (FT232H in sync FIFO mode); this selects the sync bridge.
    // (A toggle flop in the CLKOUT domain is watched instead of CLKOUT itself, so
    // the CLKOUT net only feeds clock pins and can use global clock routing.)
    wire fclk = ft_clkout;
    reg  ftog = 1'b0;
    always @(posedge fclk)
        ftog <= ~ftog;
    wire [2:0] ftx_s;
    sync2 #(.W(3)) u_ftx (.clk(clk), .d({ftog, ft_siwu_n, ft_oe_n}), .q(ftx_s));
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
    wire sync_mode = (clk_idle != 16'hFFFF);

    // The CLKOUT-domain halves of the sync FIFOs freeze when CLKOUT stops, so
    // the 27 MHz halves stay in reset until CLKOUT has run for 64 cycles; by
    // then the CLKOUT side has been reset as well (frst below).
    reg [6:0] sync_age;
    always @(posedge clk)
        if (rst || !sync_mode)
            sync_age <= 7'd0;
        else if (!sync_age[6])
            sync_age <= sync_age + 1'b1;
    wire sync_ready = sync_age[6];
    wire [2:0] ft_status = {sync_mode, ftx_s[1], ftx_s[0]};

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

    // ------------------------------------------------------------ FT245 async
    wire [7:0] fta_d_out;
    wire       fta_d_oe, fta_rd_n, fta_wr_n;
    wire       frx_valid;
    wire [7:0] frx_data;
    wire       ftx_pop;
    reg        active_port;   // 0 = UART, 1 = FT232H

    ft245_async u_ft (
        .clk(clk), .rst(rst || sync_mode), .rxf_n_s(rxf_n_s), .txe_n_s(txe_n_s),
        .d_in(ft_d), .d_out(fta_d_out), .d_oe(fta_d_oe), .rd_n(fta_rd_n), .wr_n(fta_wr_n),
        .rx_space(rxf_level < ((1 << RX_AW) - 8)),
        .rx_valid(frx_valid), .rx_data(frx_data),
        .tx_avail(active_port && txf_valid && !sync_mode), .tx_data(txf_dout), .tx_pop(ftx_pop)
    );

    // ------------------------------------------------------------ FT245 sync (CLKOUT domain)
    reg  [2:0] frst_s = 3'b111;          // reset held until sync mode is seen
    always @(posedge fclk)
        frst_s <= {frst_s[1:0], rst || !sync_mode};
    wire frst = frst_s[2];

    // host -> FPGA: sync bridge -> dual-clock FIFO -> main RX FIFO
    wire       fs_rx_wr, fs_rx_room;
    wire [7:0] fs_rx_data;
    wire [7:0] srx_dout;
    wire       srx_valid, srx_more;
    wire       srx_pop;
    afifo8 #(.AW(9)) u_srx (
        .wclk(fclk), .wrst(frst), .wr(fs_rx_wr), .din(fs_rx_data), .wroom(fs_rx_room),
        .rclk(clk), .rrst(!sync_ready), .rd(srx_pop), .dout(srx_dout), .valid(srx_valid), .more(srx_more)
    );

    // FPGA -> host: main TX FIFO -> dual-clock FIFO -> sync bridge
    wire       stx_room, stx_pop;
    wire [7:0] stx_dout;
    wire       stx_valid, stx_more, fs_tx_rd;
    afifo8 #(.AW(9)) u_stx (
        .wclk(clk), .wrst(!sync_ready), .wr(stx_pop), .din(txf_dout), .wroom(stx_room),
        .rclk(fclk), .rrst(frst), .rd(fs_tx_rd), .dout(stx_dout), .valid(stx_valid), .more(stx_more)
    );
    assign stx_pop = sync_ready && active_port && txf_valid && stx_room;

    wire [7:0] fts_d_out;
    wire       fts_d_oe, fts_oe_n, fts_rd_n, fts_wr_n;
    ft245_sync u_fts (
        .clk(fclk), .rst(frst), .rxf_n(ft_rxf_n), .txe_n(ft_txe_n), .d_in(ft_d),
        .d_out(fts_d_out), .d_oe(fts_d_oe), .oe_n(fts_oe_n), .rd_n(fts_rd_n), .wr_n(fts_wr_n),
        .rx_room(fs_rx_room), .rx_wr(fs_rx_wr), .rx_data(fs_rx_data),
        .tx_valid(stx_valid), .tx_more(stx_more), .tx_data(stx_dout), .tx_rd(fs_tx_rd)
    );

    // Pin mux: the sync bridge owns the bus while CLKOUT runs and it is out of
    // reset (sync_mode also drops when CLKOUT stops and frst can no longer move).
    wire use_sync = sync_ready && !frst;
    // (one tri-state per pin: nested "? :" with 'z' would not become an IOBUF)
    wire [7:0] ft_d_o  = use_sync ? fts_d_out : fta_d_out;
    wire       ft_d_en = use_sync ? fts_d_oe  : fta_d_oe;
    assign ft_d    = ft_d_en ? ft_d_o : 8'bz;
    assign ft_rd_n = use_sync ? fts_rd_n : fta_rd_n;
    assign ft_wr_n = use_sync ? fts_wr_n : fta_wr_n;
    assign ft_oe_n = use_sync ? fts_oe_n : 1'bz;

    // ------------------------------------------------------------ port mux
    // Sync-FIFO bytes move into the main RX FIFO when no other link writes.
    assign srx_pop = sync_ready && srx_valid && !urx_valid && !frx_valid && (rxf_level < ((1 << RX_AW) - 8));

    always @(posedge clk) begin
        if (rst)
            active_port <= 1'b0;
        else if (frx_valid || srx_pop)
            active_port <= 1'b1;
        else if (urx_valid)
            active_port <= 1'b0;
    end

    assign rxf_wr  = urx_valid | frx_valid | srx_pop;
    assign rxf_din = frx_valid ? frx_data : srx_pop ? srx_dout : urx_data;
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
    assign txf_rd = utx_pop | ftx_pop | stx_pop;

    wire tx_drained = !txf_valid && (txf_level == 0) && !utx_busy && !utx_start;

    // ------------------------------------------------------------ engine
    wire       e_cle, e_ale, e_we, e_re, e_ce, e_wp_hi, e_io_oe, e_nand_park;
    wire [7:0] e_io_o;
    wire       e_cs, e_sck, e_mosi, e_io2_hi, e_io3_hi, e_spi_park, e_qin;
    wire       nand_act, spi_act, err;
    wire [2:0] t_mode;
    wire [4:0] t_sel;
    wire       t_val;
    wire [20:0] t_lv;

    engine #(.CLK_HZ(CLK_HZ), .BAUD_DIV_DEFAULT(BAUD_DIV), .RX_AW(RX_AW)) u_eng (
        .clk(clk), .rst(rst),
        .rx_valid(rxf_valid), .rx_data(rxf_dout), .rx_pop(rxf_rd),
        .tx_full(txf_full), .tx_room(txf_level < ((1 << TX_AW) - 16)), .tx_push(txf_wr), .tx_data(txf_din), .tx_drained(tx_drained),
        .nand_cle(e_cle), .nand_ale(e_ale), .nand_we_act(e_we), .nand_re_act(e_re),
        .nand_ce_act(e_ce), .nand_wp_hi(e_wp_hi), .nand_io_o(e_io_o), .nand_io_oe(e_io_oe),
        .nand_io_i(nand_io), .nand_rb(rb_s), .nand_park(e_nand_park),
        .spi_cs_act(e_cs), .spi_sck(e_sck), .spi_mosi(e_mosi),
        .spi_io_i({spi_io3, spi_io2, spi_io1, spi_io0}), .spi_qin(e_qin),
        .spi_io2_hi(e_io2_hi), .spi_io3_hi(e_io3_hi), .spi_park(e_spi_park),
        .baud_div(baud_div), .active_port(active_port), .ft_status(ft_status),
        .rx_overflow(rx_overflow),
        .test_mode(t_mode), .test_sel(t_sel), .test_val(t_val), .test_lv(t_lv),
        .nand_activity(nand_act), .spi_activity(spi_act), .err_led(err)
    );

    // ------------------------------------------------------------ pins
    // Pin test index: 0-7 NAND IO0-7, 8 CLE, 9 ALE, 10 WE#, 11 RE#, 12 CE#,
    // 13 WP#, 14 R/B#, 15 SPI CS#, 16 SCK, 17 IO0/DI, 18 IO1/DO, 19 IO2, 20 IO3.
    // In test mode every one of these pins is released except the selected one.
    // The test controls are registered so that each pad's OE and O come from a
    // single LUT fed by flip-flops, and a released pin keeps its normal output
    // value (only OE drops; idle levels equal the pulls): entering, leaving or
    // changing the test mode can never glitch a strobe such as WE#, RE# or CE#.
    reg         t_on;
    reg  [20:0] t_drv;
    reg         t_val_r;
    genvar gi;
    always @(posedge clk) begin
        if (rst) begin
            t_on    <= 1'b0;
            t_drv   <= 21'd0;
            t_val_r <= 1'b0;
        end else begin
            t_on    <= (t_mode != 3'd0);
            t_drv   <= (t_mode >= 3'd2 && t_sel < 5'd21) ? (21'd1 << t_sel) : 21'd0;
            t_val_r <= t_val;
        end
    end

    // Normal-mode output enables and values, then one tri-state per pin.
    wire [20:0] n_oe = {
        !(e_spi_park || e_qin), !(e_spi_park || e_qin), 1'b0, !(e_spi_park || e_qin),
        !e_spi_park, !e_spi_park,
        1'b0, !e_nand_park, !e_nand_park, !e_nand_park, !e_nand_park, !e_nand_park, !e_nand_park,
        {8{e_io_oe && !e_nand_park}}};
    wire [20:0] n_o = {
        e_io3_hi, e_io2_hi, 1'b1, e_mosi,
        e_sck, ~e_cs,
        1'b1, e_wp_hi, ~e_ce, ~e_re, ~e_we, e_ale, e_cle,
        e_io_o};
    wire [20:0] p_oe = t_on ? t_drv : n_oe;
    wire [20:0] p_o  = (t_drv & {21{t_val_r}}) | (~t_drv & n_o);

    generate
        for (gi = 0; gi < 8; gi = gi + 1) begin : g_nio
            assign nand_io[gi] = p_oe[gi] ? p_o[gi] : 1'bz;
        end
    endgenerate
    assign nand_cle  = p_oe[8]  ? p_o[8]  : 1'bz;
    assign nand_ale  = p_oe[9]  ? p_o[9]  : 1'bz;
    assign nand_we_n = p_oe[10] ? p_o[10] : 1'bz;
    assign nand_re_n = p_oe[11] ? p_o[11] : 1'bz;
    assign nand_ce_n = p_oe[12] ? p_o[12] : 1'bz;
    assign nand_wp_n = p_oe[13] ? p_o[13] : 1'bz;
    assign nand_rb_n = p_oe[14] ? p_o[14] : 1'bz;
    assign spi_cs_n  = p_oe[15] ? p_o[15] : 1'bz;
    assign spi_sck   = p_oe[16] ? p_o[16] : 1'bz;
    assign spi_io0   = p_oe[17] ? p_o[17] : 1'bz;
    assign spi_io1   = p_oe[18] ? p_o[18] : 1'bz;
    assign spi_io2   = p_oe[19] ? p_o[19] : 1'bz;
    assign spi_io3   = p_oe[20] ? p_o[20] : 1'bz;

    sync2 #(.W(21)) u_tlv (
        .clk(clk),
        .d({spi_io3, spi_io2, spi_io1, spi_io0, spi_sck, spi_cs_n, nand_rb_n, nand_wp_n,
            nand_ce_n, nand_re_n, nand_we_n, nand_ale, nand_cle, nand_io}),
        .q(t_lv)
    );

    // ------------------------------------------------------------ LEDs (active low)
    reg [23:0] hb;
    always @(posedge clk) hb <= hb + 1'b1;

    wire led_uart, led_ft, led_nand, led_spi;
    pulse_stretch u_ls0 (.clk(clk), .in(urx_valid | utx_start), .out(led_uart));
    pulse_stretch u_ls1 (.clk(clk), .in(frx_valid | ftx_pop | srx_pop | stx_pop), .out(led_ft));
    pulse_stretch u_ls2 (.clk(clk), .in(nand_act),              .out(led_nand));
    pulse_stretch u_ls3 (.clk(clk), .in(spi_act),               .out(led_spi));

    assign led_n = ~{err, led_spi, led_nand, led_ft, led_uart, hb[23]};
endmodule
