// Simulation top: FPGA design + NAND and SPI flash models.
// Host links (UART and FT232H FIFO) are driven from cocotb (test_rtl.py).
`timescale 1ns/1ps

module tb;
    reg clk27 = 1'b0;
    always #18.5185 clk27 = ~clk27;      // 27 MHz

    reg  [1:0] btn_n = 2'b11;
    wire [5:0] led_n;

    wire uart_tx;
    reg  uart_rx = 1'b1;

    wire [7:0] nand_io;
    wire nand_cle, nand_ale, nand_we_n, nand_re_n, nand_ce_n, nand_wp_n, nand_rb_n;

    wire spi_cs_n, spi_sck, spi_io0, spi_io1, spi_io2, spi_io3;

    wire [7:0] ft_d;
    reg  ft_rxf_n = 1'b1;
    reg  ft_txe_n = 1'b1;
    wire ft_rd_n, ft_wr_n;
    reg  [7:0] ft_d_host = 8'h00;
    reg  ft_d_host_oe = 1'b0;
    assign ft_d = ft_d_host_oe ? ft_d_host : 8'bz;

    // Board / wiring pull-ups (the design also enables FPGA-internal ones)
    pullup (nand_rb_n);
    pullup (spi_io1);
    genvar gi;
    generate
        for (gi = 0; gi < 8; gi = gi + 1) begin : pu
            pullup (nand_io[gi]);
            pullup (ft_d[gi]);
        end
    endgenerate

    top #(.CLK_HZ(27_000_000)) dut (
        .clk27(clk27), .btn_n(btn_n), .led_n(led_n),
        .uart_tx(uart_tx), .uart_rx(uart_rx),
        .nand_io(nand_io), .nand_cle(nand_cle), .nand_ale(nand_ale),
        .nand_we_n(nand_we_n), .nand_re_n(nand_re_n), .nand_ce_n(nand_ce_n),
        .nand_wp_n(nand_wp_n), .nand_rb_n(nand_rb_n),
        .spi_cs_n(spi_cs_n), .spi_sck(spi_sck), .spi_io0(spi_io0), .spi_io1(spi_io1),
        .spi_io2(spi_io2), .spi_io3(spi_io3),
        .ft_d(ft_d), .ft_rxf_n(ft_rxf_n), .ft_txe_n(ft_txe_n),
        .ft_rd_n(ft_rd_n), .ft_wr_n(ft_wr_n)
    );

    nand_model u_nand (
        .ce_n(nand_ce_n), .cle(nand_cle), .ale(nand_ale), .we_n(nand_we_n),
        .re_n(nand_re_n), .wp_n(nand_wp_n), .io(nand_io), .rb_n(nand_rb_n)
    );

`ifdef SPI_NAND
    spi_nand_model u_spi (
        .cs_n(spi_cs_n), .sck(spi_sck), .mosi(spi_io0), .miso(spi_io1),
        .wp_n(spi_io2), .hold_n(spi_io3)
    );
`else
    spi_nor_model u_spi (
        .cs_n(spi_cs_n), .sck(spi_sck), .mosi(spi_io0), .miso(spi_io1),
        .wp_n(spi_io2), .hold_n(spi_io3)
    );
`endif

    // Bus contention check on the FT232H data bus
    always @(negedge ft_rd_n)
        if (dut.ft_d_oe) $display("[tb] ERROR: FPGA drives ft_d while RD# is low");
endmodule
